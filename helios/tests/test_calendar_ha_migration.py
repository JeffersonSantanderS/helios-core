"""Helios v5 — Calendar HA-first Migration Tests.

Tests for Home Assistant calendar integration in CalendarModule.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from helios.modules.calendar import (
    MODULE_NAME,
    SOURCE,
    CalendarModule,
    _deterministic_event_id,
    _sanitize_title,
)

# ---------------------------------------------------------------------------
# DDL
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

MOCK_HA_CONFIG_DATA = {
    "home_assistant": {
        "enabled": True,
        "base_url": "http://homeassistant.local:8123",
        "token_env": "HASS_TOKEN",
        "timeout": 15,
        "calendar": {
            "enabled": True,
            "source": "home_assistant",
            "lookahead_days": 7,
            "entities": [
                "calendar.jefferson",
                "calendar.work",
            ],
        },
    },
    "fallbacks": {
        "icloud": {"enabled": True},
        "pyicloud": {"enabled": True},
    },
}

MOCK_HA_CONFIG_DISABLED = {
    "home_assistant": {"enabled": False},
    "fallbacks": {"icloud": {"enabled": True}, "pyicloud": {"enabled": True}},
}

MOCK_HA_CALENDAR_EVENTS = [
    {
        "summary": "Team Standup",
        "start": {"dateTime": "2026-05-15T09:00:00+00:00"},
        "end": {"dateTime": "2026-05-15T09:30:00+00:00"},
        "location": "Zoom",
        "uid": "ha-evt-001",
    },
    {
        "summary": "All Day Meeting",
        "start": {"date": "2026-05-15"},
        "end": {"date": "2026-05-16"},
        "location": "",
    },
]


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


def _insert_event(db_path: str, icloud_id: str, title: str,
                  start_time: str, end_time: str,
                  is_all_day: int = 0, busy_free: str = "busy", source: str = "home_assistant") -> None:
    """Helper: insert a single event directly into calendar_events."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO calendar_events
            (icloud_id, title, location, start_time, end_time, is_all_day, busy_free, source)
        VALUES (?, ?, '', ?, ?, ?, ?, ?)""",
        (icloud_id, title, start_time, end_time, is_all_day, busy_free, source),
    )
    conn.commit()
    conn.close()


# ===================================================================
# 1. HA event normalization
# ===================================================================

class TestHANormalization:
    def test_normalize_ha_event_timed(self, cal):
        """HA timed event normalizes to calendar_events schema."""
        ha_evt = {
            "summary": "Sprint Planning",
            "start": {"dateTime": "2026-05-15T10:00:00+00:00"},
            "end": {"dateTime": "2026-05-15T11:00:00+00:00"},
            "location": "Conf Room A",
            "uid": "ha-sprint-1",
        }
        result = cal._normalize_ha_event(ha_evt, "calendar.work")
        assert result is not None
        assert result["title"] == "Sprint Planning"
        assert result["source"] == "home_assistant"
        assert result["icloud_id"] == "ha-sprint-1"
        assert result["is_all_day"] == 0
        assert result["busy_free"] == "busy"
        assert result["location"] == "Conf Room A"

    def test_normalize_ha_event_allday(self, cal):
        """HA all-day event (date format) is detected correctly."""
        ha_evt = {
            "summary": "Company Holiday",
            "start": {"date": "2026-05-15"},
            "end": {"date": "2026-05-16"},
        }
        result = cal._normalize_ha_event(ha_evt, "calendar.jefferson")
        assert result is not None
        assert result["is_all_day"] == 1
        assert result["icloud_id"].startswith("ha_")

    def test_normalize_ha_event_no_uid(self, cal):
        """HA event without uid gets deterministic ID."""
        ha_evt = {
            "summary": "Weekly Sync",
            "start": {"dateTime": "2026-05-15T14:00:00+00:00"},
            "end": {"dateTime": "2026-05-15T15:00:00+00:00"},
        }
        result = cal._normalize_ha_event(ha_evt, "calendar.work")
        assert result is not None
        # Should use deterministic hash, not random
        assert result["icloud_id"].startswith("ha_")
        # Same input → same ID
        result2 = cal._normalize_ha_event(ha_evt, "calendar.work")
        assert result2["icloud_id"] == result["icloud_id"]

    def test_normalize_ha_event_title_sanitized(self, cal):
        """HA events with sensitive titles are sanitized."""
        ha_evt = {
            "summary": "Reset password for API key",
            "start": {"dateTime": "2026-05-15T10:00:00+00:00"},
            "end": {"dateTime": "2026-05-15T11:00:00+00:00"},
        }
        result = cal._normalize_ha_event(ha_evt, "calendar.jefferson")
        assert result is not None
        assert "[REDACTED]" in result["title"]

    def test_normalize_ha_event_missing_times(self, cal):
        """HA event missing start/end returns None."""
        ha_evt = {"summary": "Bad Event"}
        result = cal._normalize_ha_event(ha_evt, "calendar.jefferson")
        assert result is None


# ===================================================================
# 2. Deterministic event ID
# ===================================================================

class TestDeterministicEventID:
    def test_same_input_same_id(self):
        """Same inputs always produce the same ID."""
        id1 = _deterministic_event_id("calendar.work", "2026-05-15T10:00:00+00:00", "2026-05-15T11:00:00+00:00", "Meeting")
        id2 = _deterministic_event_id("calendar.work", "2026-05-15T10:00:00+00:00", "2026-05-15T11:00:00+00:00", "Meeting")
        assert id1 == id2

    def test_different_input_different_id(self):
        """Different inputs produce different IDs."""
        id1 = _deterministic_event_id("calendar.work", "2026-05-15T10:00:00+00:00", "2026-05-15T11:00:00+00:00", "Meeting A")
        id2 = _deterministic_event_id("calendar.work", "2026-05-15T10:00:00+00:00", "2026-05-15T11:00:00+00:00", "Meeting B")
        assert id1 != id2

    def test_starts_with_ha_prefix(self):
        """Deterministic IDs start with 'ha_'."""
        eid = _deterministic_event_id("cal", "start", "end", "title")
        assert eid.startswith("ha_")


# ===================================================================
# 3. HA-first tick with config
# ===================================================================

class TestHAFirstTick:
    def test_ha_calendar_fetch(self, db_path):
        """Calendar tick fetches from HA when enabled."""
        config = {
            "ha_enabled": True,
            "ha_base_url": "http://homeassistant.local:8123",
            "ha_token": "test-token",
            "ha_calendars": ["calendar.jefferson", "calendar.work"],
            "fallback_enabled": True,
        }

        with patch("helios.modules.calendar.PYICLOUD_AVAILABLE", False), \
             patch("helios.modules.calendar.HA_CLIENT_AVAILABLE", True), \
             patch("helios.modules.calendar._ha_fetch_calendar", return_value=MOCK_HA_CALENDAR_EVENTS):

            cal = CalendarModule(db_path, config)
            result = cal.tick()

        assert result["source"] == "home_assistant"
        assert result["fallback_used"] is False
        assert result["sync_count"] == 4  # 2 calendars × 2 events from HA

    def test_ha_unavailable_uses_icloud_fallback(self, db_path):
        """When HA returns no events and fallback enabled, falls back."""
        config = {
            "ha_enabled": True,
            "ha_base_url": "http://homeassistant.local:8123",
            "ha_token": "test-token",
            "ha_calendars": ["calendar.jefferson", "calendar.work"],
            "fallback_enabled": True,
        }

        with patch("helios.modules.calendar.PYICLOUD_AVAILABLE", False), \
             patch("helios.modules.calendar.HA_CLIENT_AVAILABLE", True), \
             patch("helios.modules.calendar._ha_fetch_calendar", return_value=[]):

            cal = CalendarModule(db_path, config)
            result = cal.tick()

        # No HA events, no iCloud → cache
        assert result["source"] == "cache"

    def test_ha_disabled_uses_fallback(self, db_path):
        """When HA is disabled in config, skips HA entirely."""
        config = {
            "ha_enabled": False,
            "fallback_enabled": True,
        }

        with patch("helios.modules.calendar.PYICLOUD_AVAILABLE", False):
            cal = CalendarModule(db_path, config)
            result = cal.tick()

        # No HA, no iCloud → cache
        assert result["source"] == "cache"


# ===================================================================
# 4. Context keys preserved
# ===================================================================

class TestContextPreserved:
    def test_context_keys_after_ha_sync(self, db_path):
        """HA-synced events still produce all 7 context keys."""
        config = {
            "ha_enabled": True,
            "ha_base_url": "http://homeassistant.local:8123",
            "ha_token": "test-token",
            "ha_calendars": ["calendar.jefferson", "calendar.work"],
            "fallback_enabled": True,
        }

        # Insert a "today" HA event directly
        now = datetime.now(timezone.utc)
        _insert_event(
            db_path,
            "ha_test-001",
            "HA Morning Standup",
            now.replace(hour=9, minute=0, second=0, microsecond=0).isoformat(),
            now.replace(hour=9, minute=30, second=0, microsecond=0).isoformat(),
            source="home_assistant",
        )

        with patch("helios.modules.calendar.PYICLOUD_AVAILABLE", False), \
             patch("helios.modules.calendar.HA_CLIENT_AVAILABLE", True), \
             patch("helios.modules.calendar._ha_fetch_calendar", return_value=[]):

            cal = CalendarModule(db_path, config)
            result = cal.tick()

        assert result["context_written"] == 7

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

    def test_ha_events_upsert_into_calendar_events(self, db_path):
        """HA events upsert into the same calendar_events table."""
        config = {
            "ha_enabled": True,
            "ha_base_url": "http://homeassistant.local:8123",
            "ha_token": "test-token",
            "ha_calendars": ["calendar.jefferson", "calendar.work"],
            "fallback_enabled": True,
        }

        with patch("helios.modules.calendar.PYICLOUD_AVAILABLE", False), \
             patch("helios.modules.calendar.HA_CLIENT_AVAILABLE", True), \
             patch("helios.modules.calendar._ha_fetch_calendar", return_value=MOCK_HA_CALENDAR_EVENTS):

            cal = CalendarModule(db_path, config)
            cal.tick()

        # Check events were written with source=home_assistant
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT source FROM calendar_events WHERE source = 'home_assistant'").fetchall()
        conn.close()

        assert len(rows) == 3  # Same mock data for both calendars: "evt-001" dedupes across calendars


# ===================================================================
# 5. Fallback disabled = degraded when HA fails
# ===================================================================

class TestFallbackConfig:
    def test_fallback_disabled_ha_fails_reports_stale(self, db_path):
        """When fallback disabled and HA fails, reports cache/stale."""
        config = {
            "ha_enabled": True,
            "ha_base_url": "http://homeassistant.local:8123",
            "ha_token": "test-token",
            "ha_calendars": ["calendar.jefferson", "calendar.work"],
            "fallback_enabled": False,
        }

        with patch("helios.modules.calendar.PYICLOUD_AVAILABLE", False), \
             patch("helios.modules.calendar.HA_CLIENT_AVAILABLE", True), \
             patch("helios.modules.calendar._ha_fetch_calendar", return_value=[]):

            cal = CalendarModule(db_path, config)
            result = cal.tick()

        assert result["source"] == "cache"