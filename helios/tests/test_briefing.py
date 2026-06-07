"""Helios v5 — Briefing Module Tests.

Comprehensive pytest tests for helios.modules.briefing.BriefingModule.
Uses tmp_path for temporary SQLite databases.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from helios.modules.briefing import (
    MODULE_NAME,
    SOURCE,
    BriefingModule,
    _format_time,
    _now_utc,
    _parse_iso8601,
    _progress_bar,
    _today_key,
)

# ---------------------------------------------------------------------------
# DDL helpers — create all tables the module needs
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

_MOOD_DDL = """
CREATE TABLE IF NOT EXISTS mood (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    emoji           TEXT    NOT NULL,
    score           INTEGER NOT NULL CHECK (score BETWEEN 1 AND 10),
    note            TEXT,
    source          TEXT    NOT NULL DEFAULT 'discord_button',
    discord_msg_id  TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

_FOCUS_DDL = """
CREATE TABLE IF NOT EXISTS focus (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    state           TEXT    NOT NULL CHECK (state IN ('working','gaming','idle','meeting','break')),
    source          TEXT    NOT NULL,
    context         TEXT    NOT NULL DEFAULT '{}',
    duration_secs   INTEGER,
    session_start   TEXT,
    session_end     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

_HABITS_DDL = """
CREATE TABLE IF NOT EXISTS habits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT    NOT NULL UNIQUE,
    description     TEXT,
    frequency       TEXT    NOT NULL DEFAULT 'daily',
    target_count    INTEGER NOT NULL DEFAULT 1,
    current_streak  INTEGER NOT NULL DEFAULT 0,
    longest_streak  INTEGER NOT NULL DEFAULT 0,
    last_completed  TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

_HABIT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS habit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id        INTEGER NOT NULL REFERENCES habits (id),
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    note            TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

_ALL_DDL_STATEMENTS = [
    _CONTEXT_DDL,
    _CALENDAR_EVENTS_DDL,
    _MOOD_DDL,
    _FOCUS_DDL,
    _HABITS_DDL,
    _HABIT_LOG_DDL,
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MOCK_NOW_MORNING = datetime(2026, 4, 26, 7, 30, 0, tzinfo=timezone.utc)
_MOCK_NOW_EVENING = datetime(2026, 4, 26, 21, 0, 0, tzinfo=timezone.utc)
_MOCK_NOW_OFFHOUR = datetime(2026, 4, 26, 5, 0, 0, tzinfo=timezone.utc)  # Before morning window

_TODAY_KEY = "2026-04-26"


@pytest.fixture()
def db_path(tmp_path):
    """Return a path to a temporary SQLite database with required tables."""
    p = tmp_path / "test_helios.db"
    db = p.as_posix()
    conn = sqlite3.connect(db)
    for ddl in _ALL_DDL_STATEMENTS:
        conn.executescript(ddl)
    conn.close()
    return db


@pytest.fixture()
def briefing(db_path):
    """Return a BriefingModule wired to the temp DB."""
    return BriefingModule(db_path, {
        "morning_time": "07:00",
        "evening_time": "21:00",
    })


# ---------------------------------------------------------------------------
# DB insert helpers
# ---------------------------------------------------------------------------

def _insert_calendar_event(db_path: str, icloud_id: str, title: str,
                           start_time: str, end_time: str,
                           is_all_day: int = 0, busy_free: str = "busy") -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO calendar_events
           (icloud_id, title, location, start_time, end_time, is_all_day, busy_free, source)
           VALUES (?, ?, '', ?, ?, ?, ?, 'manual')""",
        (icloud_id, title, start_time, end_time, is_all_day, busy_free),
    )
    conn.commit()
    conn.close()


def _insert_context(db_path: str, module: str, key: str, value: str,
                    source: str = "script_engine") -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO context (source, module, key, value, priority)
           VALUES (?, ?, ?, ?, 0)
           ON CONFLICT (module, key, source) DO UPDATE SET value = excluded.value""",
        (source, module, key, value),
    )
    conn.commit()
    conn.close()


def _insert_mood(db_path: str, score: int, emoji: str = "😊",
                 ts: str = "2026-04-26T12:00:00Z", source: str = "discord_button") -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mood (ts, emoji, score, source) VALUES (?, ?, ?, ?)",
        (ts, emoji, score, source),
    )
    conn.commit()
    conn.close()


def _insert_focus(db_path: str, state: str, duration_secs: int = 3600,
                  source: str = "calendar",
                  ts: str = "2026-04-26T10:00:00Z") -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO focus (ts, state, source, context, duration_secs) VALUES (?, ?, ?, '{}', ?)",
        (ts, state, source, duration_secs),
    )
    conn.commit()
    conn.close()


def _insert_habit(db_path: str, slug: str, description: str = "",
                  streak: int = 0, longest: int = 0) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """INSERT INTO habits (slug, description, current_streak, longest_streak)
           VALUES (?, ?, ?, ?)""",
        (slug, description, streak, longest),
    )
    conn.commit()
    hid = cur.lastrowid
    conn.close()
    return hid


def _insert_habit_log(db_path: str, habit_id: int,
                      ts: str = "2026-04-26T07:00:00Z") -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO habit_log (habit_id, ts) VALUES (?, ?)",
        (habit_id, ts),
    )
    conn.commit()
    conn.close()


# ===================================================================
# 1. test_progress_bar
# ===================================================================

def test_progress_bar():
    """progress bar fills correctly for various ratios."""
    assert _progress_bar(0.5, 1.0) == "█████░░░░░"
    assert _progress_bar(75, 100) == "███████░░░"
    assert _progress_bar(0, 100) == "░░░░░░░░░░"
    assert _progress_bar(100, 100) == "██████████"
    assert _progress_bar(0, 0) == "░░░░░░░░░░"


# ===================================================================
# 2. test_format_time
# ===================================================================

def test_format_time():
    """format_time extracts HH:MM from ISO string."""
    assert _format_time("2026-04-26T09:30:00+00:00") == "09:30"
    assert _format_time("2026-04-26T21:00:00Z") == "21:00"


# ===================================================================
# 3. test_parse_iso8601
# ===================================================================

def test_parse_iso8601():
    """parse_iso8601 handles various ISO formats."""
    dt = _parse_iso8601("2026-04-26T09:30:00+00:00")
    assert dt.hour == 9
    assert dt.minute == 30

    dt_z = _parse_iso8601("2026-04-26T21:00:00Z")
    assert dt_z.hour == 21

    dt_empty = _parse_iso8601("")
    assert dt_empty == datetime.min.replace(tzinfo=timezone.utc)


# ===================================================================
# 4. test_today_key
# ===================================================================

def test_today_key():
    """today_key returns YYYY-MM-DD."""
    mock_now = datetime(2026, 4, 26, 7, 30, 0, tzinfo=timezone.utc)
    with patch("helios.modules.briefing._now_utc", return_value=mock_now):
        assert _today_key() == "2026-04-26"


# ===================================================================
# 5. test_module_init
# ===================================================================

def test_module_init(briefing):
    """BriefingModule initializes with expected defaults."""
    assert briefing.db_path is not None
    assert briefing._morning_time == "07:00"
    assert briefing._evening_time == "21:00"
    assert briefing._format == "short"
    assert briefing._insight_enabled is True


# ===================================================================
# 6. test_ensure_tables
# ===================================================================

def test_ensure_tables(db_path):
    """_ensure_tables creates the briefing_log table."""
    mod = BriefingModule(db_path, {})
    conn = sqlite3.connect(db_path)
    # briefing_log should exist after init
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='briefing_log'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1


# ===================================================================
# 7. test_generate_morning_basic
# ===================================================================

def test_generate_morning_basic(briefing, db_path):
    """generate_morning returns embed dict with expected keys."""
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(False))
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.generate_morning()

    assert "embed" in result
    assert "already_sent" in result
    assert "content" in result
    assert result["already_sent"] is False
    assert result["embed"] is not None


# ===================================================================
# 8. test_generate_morning_with_events
# ===================================================================

def test_generate_morning_with_events(briefing, db_path):
    """Morning briefing includes calendar events in embed."""
    _insert_calendar_event(db_path, "evt-1", "Morning Standup",
                           "2026-04-26T09:00:00+00:00", "2026-04-26T09:30:00+00:00")
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(True))
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(1))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY), \
         patch("helios.modules.briefing._now_utc", return_value=_MOCK_NOW_MORNING):
        result = briefing.generate_morning()

    assert result["embed"] is not None
    # Embed should mention the event in fields
    fields = result["embed"].get("fields", [])
    field_text = " ".join(f.get("value", "") for f in fields)
    assert "Morning Standup" in field_text or "1" in field_text


# ===================================================================
# 9. test_generate_morning_dedup
# ===================================================================

def test_generate_morning_dedup(briefing, db_path):
    """Second generate_morning call returns already_sent=True after logging."""
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(False))
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY), \
         patch("helios.modules.briefing._now_utc", return_value=_MOCK_NOW_MORNING):
        result1 = briefing.generate_morning()
        assert result1["already_sent"] is False

        # Manually log the briefing (simulating tick() behavior)
        briefing._log_briefing("morning")

        result2 = briefing.generate_morning()
        assert result2["already_sent"] is True
        assert result2["embed"] is None


# ===================================================================
# 10. test_generate_evening_basic
# ===================================================================

def test_generate_evening_basic(briefing, db_path):
    """generate_evening returns embed dict with expected keys."""
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(False))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.generate_evening()

    assert "embed" in result
    assert "already_sent" in result
    assert "content" in result
    assert result["already_sent"] is False
    assert result["embed"] is not None


# ===================================================================
# 11. test_generate_evening_with_events
# ===================================================================

def test_generate_evening_with_events(briefing, db_path):
    """Evening debrief includes calendar recap in embed."""
    _insert_calendar_event(db_path, "evt-1", "Sprint Planning",
                           "2026-04-26T10:00:00+00:00", "2026-04-26T11:00:00+00:00",
                           busy_free="busy")
    _insert_calendar_event(db_path, "evt-2", "Lunch",
                           "2026-04-26T12:00:00+00:00", "2026-04-26T13:00:00+00:00",
                           busy_free="free")
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(2))
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(True))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.generate_evening()

    assert result["embed"] is not None
    fields = result["embed"].get("fields", [])
    field_text = " ".join(f.get("value", "") for f in fields)
    assert "2" in field_text


# ===================================================================
# 12. test_generate_evening_with_mood
# ===================================================================

def test_generate_evening_with_mood(briefing, db_path):
    """Evening debrief includes mood data in content."""
    _insert_mood(db_path, score=7, emoji="😊")
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY), \
         patch("helios.modules.briefing._now_utc", return_value=_MOCK_NOW_EVENING):
        result = briefing.generate_evening()

    # Mood entries should be in the content dict
    mood = result["content"].get("mood", [])
    assert len(mood) >= 1


# ===================================================================
# 13. test_generate_evening_with_habits
# ===================================================================

def test_generate_evening_with_habits(briefing, db_path):
    """Evening debrief reads habit data."""
    hid = _insert_habit(db_path, "meditation", "Daily meditation", streak=5, longest=10)
    _insert_habit_log(db_path, hid)
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.generate_evening()

    habits = result["content"].get("habits", [])
    assert len(habits) >= 1
    # Find the meditation habit
    med = next((h for h in habits if h.get("slug") == "meditation"), None)
    assert med is not None
    assert med["current_streak"] == 5


# ===================================================================
# 14. test_tick_morning_sends
# ===================================================================

def test_tick_morning_sends(briefing, db_path):
    """During morning window, tick() generates morning briefing."""
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(False))
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._now_utc", return_value=_MOCK_NOW_MORNING), \
         patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.tick()

    assert result["generated"] == "morning"
    assert result["sent"] is True
    assert result["already_sent"] is False


# ===================================================================
# 15. test_tick_evening_sends
# ===================================================================

def test_tick_evening_sends(briefing, db_path):
    """During evening window, tick() generates evening debrief."""
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(False))

    with patch("helios.modules.briefing._now_utc", return_value=_MOCK_NOW_EVENING), \
         patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.tick()

    assert result["generated"] == "evening"
    assert result["sent"] is True
    assert result["already_sent"] is False


# ===================================================================
# 16. test_tick_offhour
# ===================================================================

def test_tick_offhour(briefing, db_path):
    """Outside briefing windows, tick() does nothing."""
    with patch("helios.modules.briefing._now_utc", return_value=_MOCK_NOW_OFFHOUR):
        result = briefing.tick()

    assert result["generated"] == "none"
    assert result["sent"] is False


# ===================================================================
# 17. test_tick_dedup
# ===================================================================

def test_tick_dedup(briefing, db_path):
    """Second tick in the same window doesn't send a duplicate briefing."""
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(False))
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._now_utc", return_value=_MOCK_NOW_MORNING), \
         patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result1 = briefing.tick()

    # today_key could change between calls if we don't patch it, so patch again
    with patch("helios.modules.briefing._now_utc", return_value=_MOCK_NOW_MORNING), \
         patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result2 = briefing.tick()

    assert result1["sent"] is True
    assert result2["sent"] is False
    assert result2["already_sent"] is True


# ===================================================================
# 18. test_briefing_log_written
# ===================================================================

def test_briefing_log_written(briefing, db_path):
    """Tick() writes to briefing_log table when sending."""
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(False))
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._now_utc", return_value=_MOCK_NOW_MORNING), \
         patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        briefing.tick()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT briefing_type, status, date_key FROM briefing_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["briefing_type"] == "morning"
    assert row["status"] == "sent"
    assert row["date_key"] == _TODAY_KEY


# ===================================================================
# 19. test_briefing_log_dedup_constraint
# ===================================================================

def test_briefing_log_dedup_constraint(briefing, db_path):
    """UNIQUE constraint on (briefing_type, date_key) prevents duplicate rows."""
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(False))
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._now_utc", return_value=_MOCK_NOW_MORNING), \
         patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        briefing.tick()
        briefing.tick()  # second tick — _log_briefing uses UPSERT so no crash

    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM briefing_log WHERE briefing_type='morning' AND date_key=?",
        (_TODAY_KEY,),
    ).fetchone()[0]
    conn.close()

    assert count == 1  # only one row despite two tick calls


# ===================================================================
# 20. test_status
# ===================================================================

def test_status(briefing):
    """status() returns module status dict."""
    status = briefing.status()
    assert status["module"] == MODULE_NAME
    assert status["morning_time"] == "07:00"
    assert status["evening_time"] == "21:00"
    assert status["format"] == "short"


# ===================================================================
# 21. test_embed_morning_has_color
# ===================================================================

def test_embed_morning_has_color(briefing, db_path):
    """Morning embed has color field set."""
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(False))
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.generate_morning()

    embed = result["embed"]
    assert embed is not None
    assert "color" in embed
    assert embed["color"] == 0x3498DB  # COLOR_BRIEFING


# ===================================================================
# 22. test_embed_evening_has_color
# ===================================================================

def test_embed_evening_has_color(briefing, db_path):
    """Evening embed has color field set."""
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.generate_evening()

    embed = result["embed"]
    assert embed is not None
    assert "color" in embed
    assert embed["color"] == 0x2ECC71  # COLOR_DEBRIEF


# ===================================================================
# 23. test_config_override
# ===================================================================

def test_config_override(db_path):
    """Config overrides change morning/evening times."""
    mod = BriefingModule(db_path, {
        "morning_time": "06:00",
        "evening_time": "22:00",
        "format": "full",
    })
    assert mod._morning_time == "06:00"
    assert mod._evening_time == "22:00"
    assert mod._format == "full"


# ===================================================================
# 24. test_morning_embed_has_title
# ===================================================================

def test_morning_embed_has_title(briefing, db_path):
    """Morning embed uses 'Morning Briefing' as title."""
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(False))
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.generate_morning()

    embed = result["embed"]
    assert "title" in embed
    assert "Morning" in embed["title"]


# ===================================================================
# 25. test_evening_embed_has_title
# ===================================================================

def test_evening_embed_has_title(briefing, db_path):
    """Evening embed uses 'Evening Debrief' as title."""
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.generate_evening()

    embed = result["embed"]
    assert "title" in embed
    assert "Evening" in embed["title"]


# ===================================================================
# 26. test_morning_with_weather
# ===================================================================

def test_morning_with_weather(briefing, db_path):
    """Morning briefing includes weather from context."""
    _insert_context(db_path, "weather", "weather.temperature", json.dumps(18))
    _insert_context(db_path, "weather", "weather.conditions", json.dumps("Partly cloudy"))
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(False))
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.generate_morning()

    # Weather should appear in content
    weather = result["content"].get("weather", {})
    assert "weather.temperature" in weather or "temperature" in str(weather)


# ===================================================================
# 27. test_morning_with_nutrition
# ===================================================================

def test_morning_with_nutrition(briefing, db_path):
    """Morning briefing reads protein/nutrition context."""
    _insert_context(db_path, "protein", "protein.today_total", json.dumps(85))
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(False))
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.generate_morning()

    nutrition = result["content"].get("nutrition", {})
    assert "protein.today_total" in nutrition or len(nutrition) > 0


# ===================================================================
# 28. test_evening_with_focus
# ===================================================================

def test_evening_with_focus(briefing, db_path):
    """Evening debrief reads focus summary."""
    _insert_focus(db_path, "working", 7200, source="calendar")
    _insert_focus(db_path, "gaming", 3600, source="gaming_detection")
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.generate_evening()

    focus = result["content"].get("focus", {})
    assert len(focus) > 0


# ===================================================================
# 29. test_morning_embed_fields_structure
# ===================================================================

def test_morning_embed_fields_structure(briefing, db_path):
    """Morning embed has valid Discord embed field structure."""
    _insert_context(db_path, "calendar", "calendar.busy_today", json.dumps(False))
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.generate_morning()

    embed = result["embed"]
    assert "fields" in embed
    for field in embed["fields"]:
        assert "name" in field
        assert "value" in field
        assert "inline" in field


# ===================================================================
# 30. test_evening_embed_fields_structure
# ===================================================================

def test_evening_embed_fields_structure(briefing, db_path):
    """Evening embed has valid Discord embed field structure."""
    _insert_context(db_path, "calendar", "calendar.today_event_count", json.dumps(0))

    with patch("helios.modules.briefing._today_key", return_value=_TODAY_KEY):
        result = briefing.generate_evening()

    embed = result["embed"]
    assert "fields" in embed
    for field in embed["fields"]:
        assert "name" in field
        assert "value" in field