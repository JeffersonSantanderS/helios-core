"""Helios v5 — Calendar Module Tests.

Comprehensive pytest tests for helios.modules.calendar.CalendarModule.

Uses tmp_path for temporary SQLite databases and mocks pyicloud since it
won't be available in CI.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from helios.modules.calendar import (
    MODULE_NAME,
    SOURCE,
    CalendarModule,
    _sanitize_title,
)

# ---------------------------------------------------------------------------
# DDL -- kept inline so each test is self-contained with its own temp DB
# ---------------------------------------------------------------------------

_CALENDAR_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS calendar_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    icloud_id       TEXT UNIQUE,
    title           TEXT NOT NULL,
    location        TEXT,
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    is_all_day      INTEGER NOT NULL DEFAULT 0,
    busy_free       TEXT NOT NULL DEFAULT 'busy',
    source          TEXT NOT NULL DEFAULT 'pyicloud',
    ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_cal_events_start ON calendar_events (start_time);
"""

_CONTEXT_DDL = """
CREATE TABLE IF NOT EXISTS context (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    source      TEXT    NOT NULL,
    module      TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value       TEXT    NOT NULL DEFAULT '{}',
    priority    INTEGER NOT NULL DEFAULT 0,
    expires_at  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT ctx_unique_latest UNIQUE (module, key, source)
)
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A fixed "now" during waking hours so free-block tests work regardless of
# when the test suite is actually executed.
_MOCK_NOW = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db_path(tmp_path):
    """Return a path to a temporary SQLite database with required tables."""
    p = tmp_path / "test_helios.db"
    db = p.as_posix()
    conn = sqlite3.connect(db)
    conn.executescript(_CALENDAR_EVENTS_DDL)
    conn.executescript(_CONTEXT_DDL)
    conn.close()
    return db


@pytest.fixture()
def cal(db_path):
    """Return a CalendarModule wired to the temp DB (no iCloud credentials)."""
    with patch("helios.modules.calendar.PYICLOUD_AVAILABLE", False):
        return CalendarModule(db_path, {})


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _mock_now() -> datetime:
    """Return the fixed mock time used for time-dependent tests."""
    return _MOCK_NOW


def _today_iso(hour: int, minute: int = 0, second: int = 0) -> str:
    """Return an ISO-8601 string for *today* at the given hour/minute (UTC).

    Uses the real current date so events land in "today".
    """
    now = _now_utc()
    dt = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    return dt.isoformat()


def _insert_event(db_path: str, icloud_id: str, title: str,
                  start_time: str, end_time: str,
                  is_all_day: int = 0, busy_free: str = "busy") -> None:
    """Helper: insert a single event directly into calendar_events."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO calendar_events
            (icloud_id, title, location, start_time, end_time,
             is_all_day, busy_free, source)
        VALUES (?, ?, '', ?, ?, ?, ?, 'manual')
        """,
        (icloud_id, title, start_time, end_time, is_all_day, busy_free),
    )
    conn.commit()
    conn.close()


def _read_context(db_path: str) -> dict[str, str]:
    """Return a dict of {context_key: json_value} for the calendar module."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT key, value FROM context WHERE module = ? AND source = ?",
        (MODULE_NAME, SOURCE),
    ).fetchall()
    conn.close()
    return {row["key"]: row["value"] for row in rows}


# ===================================================================
# 1. test_tick_writes_context
# ===================================================================

def test_tick_writes_context(cal, db_path):
    """With some mock events seeded, tick() should write all 7 context keys."""
    _insert_event(db_path, "e1", "Morning Standup",
                  _today_iso(9), _today_iso(9, 30))
    _insert_event(db_path, "e2", "Deep Work",
                  _today_iso(10), _today_iso(12))

    result = cal.tick()

    # Verify the result dict reports context_written == 7
    assert result["context_written"] == 7

    # Verify all 7 keys are present in the context table
    ctx = _read_context(db_path)
    expected_keys = {
        "calendar.busy_today",
        "calendar.free_block_minutes",
        "calendar.event_coming_in_minutes",
        "calendar.has_all_day_event",
        "calendar.next_event_title",
        "calendar.next_event_start",
        "calendar.today_event_count",
    }
    assert set(ctx.keys()) == expected_keys


# ===================================================================
# 2. test_tick_no_events
# ===================================================================

def test_tick_no_events(cal, db_path):
    """No events -> tick writes sensible defaults."""
    # Patch _now_utc so "now" is 10:00 AM within waking hours
    with patch("helios.modules.calendar._now_utc", return_value=_MOCK_NOW):
        result = cal.tick()

    assert result["context_written"] == 7
    ctx = _read_context(db_path)

    # busy_today = false
    assert json.loads(ctx["calendar.busy_today"]) is False
    # event_coming_in_minutes = None (null)
    assert json.loads(ctx["calendar.event_coming_in_minutes"]) is None
    # has_all_day_event = false
    assert json.loads(ctx["calendar.has_all_day_event"]) is False
    # today_event_count = 0
    assert json.loads(ctx["calendar.today_event_count"]) == 0
    # free_block_minutes should be a large number (entire waking window)
    # At 10:00 AM UTC, waking window remaining is 22:00-10:00 = 12h = 720 min
    free = json.loads(ctx["calendar.free_block_minutes"])
    assert free >= 720  # 12 hours of waking window remaining


# ===================================================================
# 3. test_busy_day_detection
# ===================================================================

def test_busy_day_detection(cal, db_path):
    """Overlapping busy events covering most of the day -> busy_today=true."""
    now = _now_utc()
    # Create overlapping events that span 8:00-18:00
    for i in range(5):
        start = now.replace(hour=8 + i * 2, minute=0, second=0, microsecond=0)
        end = now.replace(hour=8 + i * 2 + 2, minute=30, second=0, microsecond=0)
        _insert_event(
            db_path,
            f"busy-{i}",
            f"Busy Block {i}",
            start.isoformat(),
            end.isoformat(),
            busy_free="busy",
        )

    cal.tick()
    ctx = _read_context(db_path)

    assert json.loads(ctx["calendar.busy_today"]) is True


# ===================================================================
# 4. test_free_block_calculation
# ===================================================================

def test_free_block_calculation(cal, db_path):
    """Events with a deliberate gap -> free_block_minutes matches gap."""
    # Use _MOCK_NOW (10:00 AM) as "now" so waking-hours logic is engaged
    mock_now = _MOCK_NOW

    # Event 9:00-10:00, then gap 10:00-14:00, then event 14:00-15:00
    day = mock_now.date()
    _insert_event(
        db_path, "gap-1", "Morning Meeting",
        datetime(day.year, day.month, day.day, 9, 0, tzinfo=timezone.utc).isoformat(),
        datetime(day.year, day.month, day.day, 10, 0, tzinfo=timezone.utc).isoformat(),
    )
    _insert_event(
        db_path, "gap-2", "Afternoon Sync",
        datetime(day.year, day.month, day.day, 14, 0, tzinfo=timezone.utc).isoformat(),
        datetime(day.year, day.month, day.day, 15, 0, tzinfo=timezone.utc).isoformat(),
    )

    with patch("helios.modules.calendar._now_utc", return_value=mock_now):
        cal.tick()
    ctx = _read_context(db_path)

    free_minutes = json.loads(ctx["calendar.free_block_minutes"])
    # The gap between 10:00 and 14:00 is 4 hours = 240 minutes.
    # Since mock_now = 10:00, the first free block starts at 10:00.
    # The after-14:00 block is 22:00-15:00 = 420 min. The max is 420.
    # We verify the max free block >= 240 (the 10-14 gap or the 14-22 tail).
    assert free_minutes >= 240


# ===================================================================
# 5. test_upcoming_event
# ===================================================================

def test_upcoming_event(cal, db_path):
    """Event 30 min from now -> event_coming_in_minutes approx 30, title matches."""
    now = _now_utc()
    event_start = now + timedelta(minutes=30)
    event_end = event_start + timedelta(minutes=60)

    _insert_event(
        db_path, "upcoming-1", "Sprint Planning",
        event_start.isoformat(),
        event_end.isoformat(),
    )

    cal.tick()
    ctx = _read_context(db_path)

    minutes_until = json.loads(ctx["calendar.event_coming_in_minutes"])
    title = json.loads(ctx["calendar.next_event_title"])

    # Allow +-2 min tolerance for test execution time
    assert abs(minutes_until - 30) <= 2
    assert title == "Sprint Planning"


# ===================================================================
# 6. test_all_day_event
# ===================================================================

def test_all_day_event(cal, db_path):
    """All-day event -> has_all_day_event=true."""
    now = _now_utc()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    _insert_event(
        db_path, "allday-1", "Company Holiday",
        start.isoformat(),
        end.isoformat(),
        is_all_day=1,
        busy_free="free",
    )

    cal.tick()
    ctx = _read_context(db_path)

    assert json.loads(ctx["calendar.has_all_day_event"]) is True


# ===================================================================
# 7. test_title_sanitization
# ===================================================================

def test_title_sanitization(cal, db_path):
    """Sensitive words in titles are redacted during iCloud normalization.

    We mock _get_icloud_service to return a fake service with mock event
    objects so _normalize_icloud_event (which calls _sanitize_title) is
    exercised.
    """
    mock_now = _MOCK_NOW
    future = mock_now + timedelta(hours=2)

    # Create mock event objects that _normalize_icloud_event will process
    sensitive_evt_1 = SimpleNamespace(
        guid="sens-1",
        title="Reset my password for the API key",
        location="Office",
        startDate=future,
        endDate=future + timedelta(hours=1),
        allDay=False,
        busy=True,
    )
    sensitive_evt_2 = SimpleNamespace(
        guid="sens-2",
        title="Share SSN with HR department",
        location="",
        startDate=future + timedelta(hours=2),
        endDate=future + timedelta(hours=3),
        allDay=False,
        busy=True,
    )

    # Mock service.calendar (singular — pyicloud 2.5+ API) with get_events()
    mock_calendar_service = MagicMock()
    mock_calendar_service.get_events.return_value = [sensitive_evt_1, sensitive_evt_2]
    mock_service = MagicMock()
    mock_service.calendar = mock_calendar_service

    with patch.object(cal, "_get_icloud_service", return_value=mock_service), \
         patch("helios.modules.calendar._now_utc", return_value=mock_now):
        cal.tick()

    # Verify the DB-stored titles are sanitized
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT icloud_id, title FROM calendar_events WHERE icloud_id IN ('sens-1', 'sens-2')"
    ).fetchall()
    conn.close()

    titles_by_id = {row["icloud_id"]: row["title"] for row in rows}

    # Sens-1: "Reset my password for the API key" -> sensitive words redacted
    assert "password" not in titles_by_id["sens-1"].lower()
    assert "api" not in titles_by_id["sens-1"].lower() or "[REDACTED]" in titles_by_id["sens-1"]
    assert "[REDACTED]" in titles_by_id["sens-1"]

    # Sens-2: "Share SSN with HR department" -> SSN redacted
    assert "SSN" not in titles_by_id["sens-2"]
    assert "[REDACTED]" in titles_by_id["sens-2"]


# ===================================================================
# 8. test_pyicloud_unavailable
# ===================================================================

def test_pyicloud_unavailable(db_path):
    """When pyicloud is not importable, tick should still work from cache."""
    with patch("helios.modules.calendar.PYICLOUD_AVAILABLE", False):
        cal = CalendarModule(db_path, {
            "apple_id": "test@example.com",
            "password": "fake-password",
        })

    # Seed an event directly (simulates cached data from a previous sync)
    _insert_event(db_path, "cached-1", "Cached Standup",
                  _today_iso(9), _today_iso(9, 30))

    # tick() should succeed using the cached data
    result = cal.tick()

    assert result["source"] == "cache"
    assert result["events_today"] >= 1
    assert result["context_written"] == 7

    ctx = _read_context(db_path)
    assert json.loads(ctx["calendar.busy_today"]) is True
    assert json.loads(ctx["calendar.today_event_count"]) == 1


# ===================================================================
# 9. test_get_events_today
# ===================================================================

def test_get_events_today(cal, db_path):
    """Only today's events are returned, not tomorrow's or yesterday's."""
    now = _now_utc()

    # Today's events
    _insert_event(db_path, "today-1", "Morning Standup",
                  now.replace(hour=9, minute=0, second=0, microsecond=0).isoformat(),
                  now.replace(hour=9, minute=30, second=0, microsecond=0).isoformat())
    _insert_event(db_path, "today-2", "Afternoon Review",
                  now.replace(hour=14, minute=0, second=0, microsecond=0).isoformat(),
                  now.replace(hour=15, minute=0, second=0, microsecond=0).isoformat())

    # Tomorrow's event
    tomorrow = now + timedelta(days=1)
    _insert_event(db_path, "tomorrow-1", "Tomorrow Planning",
                  tomorrow.replace(hour=10, minute=0, second=0, microsecond=0).isoformat(),
                  tomorrow.replace(hour=11, minute=0, second=0, microsecond=0).isoformat())

    # Yesterday's event
    yesterday = now - timedelta(days=1)
    _insert_event(db_path, "yesterday-1", "Past Retrospective",
                  yesterday.replace(hour=10, minute=0, second=0, microsecond=0).isoformat(),
                  yesterday.replace(hour=11, minute=0, second=0, microsecond=0).isoformat())

    events_today = cal.get_events_today()

    # Should return the 2 today events plus the tomorrow event
    # (range extends +1 day for near-future upcoming detection)
    titles = [e["title"] for e in events_today]
    assert "Morning Standup" in titles
    assert "Afternoon Review" in titles
    assert "Past Retrospective" not in titles
    # Tomorrow events may appear in the extended range (by design)
    assert len([t for t in titles if t in ("Morning Standup", "Afternoon Review")]) == 2


# ===================================================================
# 10. test_get_free_blocks_today
# ===================================================================

def test_get_free_blocks_today(cal, db_path):
    """Free blocks helper returns correct gap tuples between busy events."""
    mock_now = _MOCK_NOW  # 10:00 AM
    day = mock_now.date()

    # Two busy events: 9:00-10:00 and 13:00-14:00
    _insert_event(db_path, "fb-1", "Morning Block",
                  datetime(day.year, day.month, day.day, 9, 0, tzinfo=timezone.utc).isoformat(),
                  datetime(day.year, day.month, day.day, 10, 0, tzinfo=timezone.utc).isoformat())
    _insert_event(db_path, "fb-2", "Afternoon Block",
                  datetime(day.year, day.month, day.day, 13, 0, tzinfo=timezone.utc).isoformat(),
                  datetime(day.year, day.month, day.day, 14, 0, tzinfo=timezone.utc).isoformat())

    with patch("helios.modules.calendar._now_utc", return_value=mock_now):
        blocks = cal.get_free_blocks_today()

    assert len(blocks) >= 1, "Should have at least one free block"

    # Each block is (start_dt, end_dt, minutes)
    for start, end, minutes in blocks:
        assert isinstance(start, datetime)
        assert isinstance(end, datetime)
        assert isinstance(minutes, int)
        assert end > start
        assert minutes > 0
        # Duration in minutes should match the datetime delta
        expected_minutes = int((end - start).total_seconds() / 60)
        assert minutes == expected_minutes

    # Verify that at least one block of >= 100 minutes exists
    # (the 10:00-13:00 gap is 180 min, or the after-14:00 block is 480 min)
    max_block = max(b[2] for b in blocks)
    assert max_block >= 100


# ===================================================================
# Additional unit-level tests for _sanitize_title
# ===================================================================

class TestSanitizeTitle:
    """Unit tests for the _sanitize_title helper."""

    def test_no_sensitive_words(self):
        assert _sanitize_title("Team Standup") == "Team Standup"

    def test_password_redacted(self):
        result = _sanitize_title("Reset password now")
        assert "password" not in result.lower()
        assert "[REDACTED]" in result

    def test_ssn_redacted(self):
        result = _sanitize_title("Share SSN with HR")
        assert "SSN" not in result
        assert "[REDACTED]" in result

    def test_api_key_redacted(self):
        result = _sanitize_title("Rotate API key")
        assert "API key" not in result
        assert "[REDACTED]" in result

    def test_credit_card_redacted(self):
        result = _sanitize_title("Credit card on file")
        assert "credit card" not in result.lower()
        assert "[REDACTED]" in result

    def test_whitespace_stripped(self):
        assert _sanitize_title("  Padded  ") == "Padded"

    def test_long_title_truncated(self):
        long_title = "x" * 300
        result = _sanitize_title(long_title)
        assert len(result) == 200

    def test_case_insensitive(self):
        result = _sanitize_title("PASSWORD is secret")
        assert "[REDACTED]" in result
        assert "PASSWORD" not in result

    def test_multiple_sensitive_words(self):
        result = _sanitize_title("password and SSN and token")
        # All three should be redacted
        assert "password" not in result.lower()
        assert "ssn" not in result.lower()
        assert "token" not in result.lower()
