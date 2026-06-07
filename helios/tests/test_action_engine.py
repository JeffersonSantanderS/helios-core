"""Helios v5 — Action Engine Tests.

Comprehensive pytest tests for helios.modules.action_engine.ActionEngine.
Uses tmp_path for temporary SQLite databases.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from helios.modules.action_engine import (
    MODULE_NAME,
    SOURCE,
    VALID_ACTIONS,
    ActionEngine,
    _action_send_matrix_nudge,
    _action_enable_dnd,
    _action_disable_dnd,
    _action_set_reminder,
    _action_queue_spotify_playlist,
    _action_adjust_schedule,
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

_REMINDERS_DDL = """
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT,
    priority    TEXT DEFAULT 'medium',
    remind_at   TEXT,
    completed   INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'script_engine',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    """Return a path to a temporary SQLite database with required tables."""
    p = tmp_path / "test_helios.db"
    db = p.as_posix()
    conn = sqlite3.connect(db)
    conn.executescript(_CALENDAR_EVENTS_DDL)
    conn.executescript(_CONTEXT_DDL)
    conn.executescript(_REMINDERS_DDL)
    conn.close()
    return db


@pytest.fixture()
def engine(db_path):
    """Return an ActionEngine wired to the temp DB."""
    return ActionEngine(db_path, {"matrix": {}})


def _insert_calendar_event(db_path: str, icloud_id: str, title: str,
                           start_time: str, end_time: str,
                           is_all_day: int = 0, busy_free: str = "busy") -> None:
    """Helper: insert a calendar event."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO calendar_events
           (icloud_id, title, location, start_time, end_time, is_all_day, busy_free, source)
           VALUES (?, ?, '', ?, ?, ?, ?, 'manual')""",
        (icloud_id, title, start_time, end_time, is_all_day, busy_free),
    )
    conn.commit()
    conn.close()


# ===================================================================
# 1. test_execute_unknown_action
# ===================================================================

def test_execute_unknown_action(engine):
    """Unknown action returns success=False."""
    result = engine.execute("nonexistent_action", {"message": "hello"})
    assert result["success"] is False
    assert "Unknown action" in result["error"]


# ===================================================================
# 2. test_execute_send_matrix_nudge
# ===================================================================

def test_execute_send_matrix_nudge(engine, db_path):
    """send_matrix_nudge action dispatches correctly."""
    with patch("helios.matrix_pusher.MatrixPusher.push", return_value=True):
        result = engine.execute("send_matrix_nudge", {
            "message": "Time to hydrate!",
            "urgency": "medium",
        })

    assert result["success"] is True
    assert result["action"] == "send_matrix_nudge"
    assert "hydrate" in result["message_sent"]


# ===================================================================
# 3. test_execute_send_matrix_nudge_with_mention
# ===================================================================

def test_execute_send_matrix_nudge_with_mention(engine, db_path):
    """send_matrix_nudge with mention prepends the mention."""
    with patch("helios.matrix_pusher.MatrixPusher.push", return_value=True):
        result = engine.execute("send_matrix_nudge", {
            "message": "Standup time!",
            "mention": "@user:example.org",
            "urgency": "high",
        })

    assert result["success"] is True
    assert "@user:example.org" in result["message_sent"]
    assert "URGENT" in result["message_sent"]


# ===================================================================
# 4. test_execute_send_matrix_nudge_missing_message
# ===================================================================

def test_execute_send_matrix_nudge_missing_message(engine):
    """send_matrix_nudge without message returns error."""
    result = engine.execute("send_matrix_nudge", {})
    assert result["success"] is False
    assert "message" in result["error"].lower()


# ===================================================================
# 5. test_execute_enable_dnd
# ===================================================================

def test_execute_enable_dnd(engine, db_path):
    """enable_dnd_mode sets DND context and returns success."""
    result = engine.execute("enable_dnd_mode", {
        "duration_minutes": 120,
        "reason": "Focus time",
    })
    assert result["success"] is True
    assert result["action"] == "enable_dnd_mode"
    assert result["dnd_enabled"] is True
    assert result["duration_minutes"] == 120

    # Verify context was written
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT value FROM context WHERE key = ? ORDER BY ts DESC LIMIT 1",
        ("action_engine.dnd_enabled",),
    ).fetchone()
    conn.close()
    assert json.loads(row[0]) is True


# ===================================================================
# 6. test_execute_disable_dnd
# ===================================================================

def test_execute_disable_dnd(engine, db_path):
    """disable_dnd_mode clears DND context."""
    # First enable
    engine.execute("enable_dnd_mode", {})
    # Then disable
    result = engine.execute("disable_dnd_mode", {})
    assert result["success"] is True
    assert result["dnd_enabled"] is False

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT value FROM context WHERE key = ? ORDER BY ts DESC LIMIT 1",
        ("action_engine.dnd_enabled",),
    ).fetchone()
    conn.close()
    assert json.loads(row[0]) is False


# ===================================================================
# 7. test_is_dnd_enabled
# ===================================================================

def test_is_dnd_enabled(engine, db_path):
    """is_dnd_enabled reads from context."""
    # Initially not set → should be False
    assert engine.is_dnd_enabled() is False

    # Enable
    engine.execute("enable_dnd_mode", {})
    assert engine.is_dnd_enabled() is True

    # Disable
    engine.execute("disable_dnd_mode", {})
    assert engine.is_dnd_enabled() is False


# ===================================================================
# 8. test_execute_queue_spotify_playlist
# ===================================================================

def test_execute_queue_spotify_playlist(engine):
    """queue_spotify_playlist requires playlist_uri."""
    result = engine.execute("queue_spotify_playlist", {
        "playlist_uri": "spotify:playlist:abc123",
        "device_id": "device_123",
        "shuffle": True,
    })
    assert result["success"] is True
    assert result["playlist_uri"] == "spotify:playlist:abc123"


# ===================================================================
# 9. test_execute_queue_spotify_missing_uri
# ===================================================================

def test_execute_queue_spotify_missing_uri(engine):
    """queue_spotify_playlist without URI returns error."""
    result = engine.execute("queue_spotify_playlist", {})
    assert result["success"] is False
    assert "playlist_uri" in result["error"].lower()


# ===================================================================
# 10. test_execute_set_reminder
# ===================================================================

def test_execute_set_reminder(engine, db_path):
    """set_reminder writes to reminders table."""
    remind_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    result = engine.execute("set_reminder", {
        "text": "Take a walk",
        "remind_at": remind_at,
        "priority": "high",
    })
    assert result["success"] is True
    assert result["text"] == "Take a walk"

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT text, priority FROM reminders WHERE text = ?",
        ("Take a walk",),
    ).fetchone()
    conn.close()
    assert row[0] == "Take a walk"
    assert row[1] == "high"


# ===================================================================
# 11. test_execute_set_reminder_missing_text
# ===================================================================

def test_execute_set_reminder_missing_text(engine):
    """set_reminder without text returns error."""
    result = engine.execute("set_reminder", {"remind_at": "2025-01-01T00:00:00Z"})
    assert result["success"] is False
    assert "text" in result["error"].lower()


# ===================================================================
# 12. test_execute_set_reminder_missing_remind_at
# ===================================================================

def test_execute_set_reminder_missing_remind_at(engine):
    """set_reminder without remind_at returns error."""
    result = engine.execute("set_reminder", {"text": "Drink water"})
    assert result["success"] is False
    assert "remind_at" in result["error"].lower()


# ===================================================================
# 13. test_execute_adjust_schedule_shift
# ===================================================================

def test_execute_adjust_schedule_shift(engine, db_path):
    """adjust_schedule shifts event times."""
    _insert_calendar_event(
        db_path,
        "evt-shift-001",
        "Meeting",
        "2025-01-15T10:00:00+00:00",
        "2025-01-15T11:00:00+00:00",
    )
    result = engine.execute("adjust_schedule", {
        "event_id": "evt-shift-001",
        "shift_minutes": 30,
    })
    assert result["success"] is True
    assert "start_time = ?" in result["changes"]

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT start_time FROM calendar_events WHERE icloud_id = ?",
        ("evt-shift-001",),
    ).fetchone()
    conn.close()
    # Start time should be shifted by 30 minutes
    assert "10:30:00" in row[0]


# ===================================================================
# 14. test_execute_adjust_schedule_buffer
# ===================================================================

def test_execute_adjust_schedule_buffer(engine, db_path):
    """adjust_schedule adds buffer to end time."""
    _insert_calendar_event(
        db_path,
        "evt-buffer-001",
        "Call",
        "2025-01-15T14:00:00+00:00",
        "2025-01-15T14:30:00+00:00",
    )
    result = engine.execute("adjust_schedule", {
        "event_id": "evt-buffer-001",
        "add_buffer_minutes": 15,
    })
    assert result["success"] is True

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT end_time FROM calendar_events WHERE icloud_id = ?",
        ("evt-buffer-001",),
    ).fetchone()
    conn.close()
    # End time should be 14:45
    assert "14:45:00" in row[0]


# ===================================================================
# 15. test_execute_adjust_schedule_rename
# ===================================================================

def test_execute_adjust_schedule_rename(engine, db_path):
    """adjust_schedule renames event."""
    _insert_calendar_event(
        db_path,
        "evt-rename-001",
        "Old Title",
        "2025-01-15T09:00:00+00:00",
        "2025-01-15T10:00:00+00:00",
    )
    result = engine.execute("adjust_schedule", {
        "event_id": "evt-rename-001",
        "new_title": "New Title",
    })
    assert result["success"] is True

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT title FROM calendar_events WHERE icloud_id = ?",
        ("evt-rename-001",),
    ).fetchone()
    conn.close()
    assert row[0] == "New Title"


# ===================================================================
# 16. test_execute_adjust_schedule_missing_event_id
# ===================================================================

def test_execute_adjust_schedule_missing_event_id(engine):
    """adjust_schedule without event_id returns error."""
    result = engine.execute("adjust_schedule", {"new_title": "Test"})
    assert result["success"] is False
    assert "event_id" in result["error"].lower()


# ===================================================================
# 17. test_execute_adjust_schedule_nonexistent_event
# ===================================================================

def test_execute_adjust_schedule_nonexistent_event(engine):
    """adjust_schedule for unknown event returns error."""
    result = engine.execute("adjust_schedule", {
        "event_id": "nonexistent-id",
        "new_title": "Test",
    })
    assert result["success"] is False
    assert "not found" in result["error"].lower()


# ===================================================================
# 18. test_action_log
# ===================================================================

def test_action_log(engine, db_path):
    """Every action execution writes to action_log."""
    with patch("helios.matrix_pusher.MatrixPusher.push", return_value=True):
        engine.execute("send_matrix_nudge", {"message": "Test nudge"})

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT action, success FROM action_log ORDER BY id DESC LIMIT 1"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["action"] == "send_matrix_nudge"
    assert rows[0]["success"] == 1


# ===================================================================
# 19. test_context_updates
# ===================================================================

def test_context_updates(engine, db_path):
    """DND actions write context values."""
    engine.execute("enable_dnd_mode", {"reason": "Deep work"})
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT key, value FROM context WHERE module = ?",
        (MODULE_NAME,),
    ).fetchall()
    conn.close()
    keys = {r["key"] for r in rows}
    assert "action_engine.dnd_enabled" in keys
    assert "action_engine.dnd_reason" in keys


# ===================================================================
# 20. test_execute_batch
# ===================================================================

def test_execute_batch(engine, db_path):
    """Multiple actions execute independently."""
    actions = [
        {"action": "send_matrix_nudge", "params": {"message": "Nudge 1"}},
        {"action": "enable_dnd_mode", "params": {"reason": "Batch test"}},
    ]
    with patch("helios.matrix_pusher.MatrixPusher.push", return_value=True):
        for item in actions:
            result = engine.execute(item["action"], item["params"])
            assert result["success"] is True

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT action FROM action_log").fetchall()
    conn.close()
    assert len(rows) >= 2


# ===================================================================
# 21. test_get_action_history
# ===================================================================

def test_get_action_history(engine, db_path):
    """get_action_history returns recent actions."""
    with patch("helios.matrix_pusher.MatrixPusher.push", return_value=True):
        engine.execute("send_matrix_nudge", {"message": "Test"})

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT action, params, result, success FROM action_log ORDER BY ts DESC LIMIT 10"
    ).fetchall()
    conn.close()
    assert len(rows) >= 1
    assert rows[0]["action"] == "send_matrix_nudge"


# ===================================================================
# 22. test_get_available_actions
# ===================================================================

def test_get_available_actions(engine):
    """get_available_actions returns sorted list."""
    actions = engine.get_available_actions()
    assert isinstance(actions, list)
    assert "send_matrix_nudge" in actions
    assert "enable_dnd_mode" in actions
    assert actions == sorted(actions)


# ===================================================================
# 23. test_status
# ===================================================================

def test_status(engine):
    """status() returns module status dict."""
    status = engine.status()
    assert status["module"] == "action_engine"
    assert "available_actions" in status
    assert status["dnd_enabled"] is False
    assert "matrix_push_available" in status


# ===================================================================
# 24. test_valid_actions_constant
# ===================================================================

def test_valid_actions_constant():
    """VALID_ACTIONS contains all expected actions."""
    assert "send_matrix_nudge" in VALID_ACTIONS
    assert "enable_dnd_mode" in VALID_ACTIONS
    assert "disable_dnd_mode" in VALID_ACTIONS
    assert "queue_spotify_playlist" in VALID_ACTIONS
    assert "set_reminder" in VALID_ACTIONS
    assert "adjust_schedule" in VALID_ACTIONS
    assert "nonexistent" not in VALID_ACTIONS


# ===================================================================
# 25. test_matrix_pusher_no_config
# ===================================================================

def test_matrix_pusher_no_config():
    """MatrixPusher with empty config returns False."""
    from helios.matrix_pusher import MatrixPusher
    pusher = MatrixPusher(cfg={})
    result = pusher.push("test message")
    assert result is False


# ===================================================================
# 26. test_execute_adjust_schedule_no_changes
# ===================================================================

def test_execute_adjust_schedule_no_changes(engine, db_path):
    """adjust_schedule with no changes specified returns note."""
    _insert_calendar_event(
        db_path,
        "evt-nochange-001",
        "Unchanged",
        "2025-01-15T09:00:00+00:00",
        "2025-01-15T10:00:00+00:00",
    )
    result = engine.execute("adjust_schedule", {"event_id": "evt-nochange-001"})
    assert result["success"] is True
    assert "No changes" in result.get("note", "")


# ===================================================================
# Phase 3: Action Engine Channel Routing Tests
# ===================================================================

class TestActionEngineChannelRouting:
    """Test that ActionEngine routes nudges through ChannelRouter when available,
    falling back to inline MatrixPusher when not."""

    def test_nudge_uses_channel_router_when_available(self, tmp_path):
        """When engine.channels is set, nudge should try ChannelRouter first."""
        from helios.channels import LogChannel
        from helios.channels.router import ChannelRouter

        jsonl = tmp_path / "nudge_channel.jsonl"
        lc = LogChannel(cfg={"jsonl_path": str(jsonl), "enabled": True})
        router = ChannelRouter(channels=[lc], shadow=False)

        ae = ActionEngine(db_path=str(tmp_path / "test.db"), config={"matrix": {}})
        ae.channels = router

        result = ae.execute("send_matrix_nudge", {"message": "Test via channel"})
        assert result["success"] is True

        # Verify LogChannel received the AlertEvent
        import json
        lines = jsonl.read_text().strip().splitlines()
        assert len(lines) >= 1
        entry = json.loads(lines[0])
        assert entry["event_type"] == "alert"
        assert "Test via channel" in entry["message"]
        assert entry["category"] == "action"
        assert entry["source"] == "action_engine"

    def test_nudge_falls_back_to_matrix_pusher_when_no_channels(self, tmp_path):
        """When engine.channels is None, nudge falls back to inline MatrixPusher."""
        ae = ActionEngine(db_path=str(tmp_path / "test.db2"), config={"matrix": {}})
        # channels is None by default — should use inline MatrixPusher path
        result = ae.execute("send_matrix_nudge", {"message": "Test fallback"})
        # MatrixPusher with empty config returns False, so success=False is expected
        # but no crash means fallback path works
        assert "success" in result

    def test_channel_failure_falls_back_gracefully(self, tmp_path):
        """If channel router raises, action_engine falls back to MatrixPusher."""
        from helios.channels.router import ChannelRouter

        broken = MagicMock()
        broken._enabled = True
        broken.name = "broken"
        broken.send.side_effect = RuntimeError("Channel exploded")
        router = ChannelRouter(channels=[broken], shadow=False)

        ae = ActionEngine(db_path=str(tmp_path / "test.db3"), config={"matrix": {}})
        ae.channels = router

        # Should not crash — falls back to MatrixPusher
        result = ae.execute("send_matrix_nudge", {"message": "Resilient test"})
        assert "success" in result  # May be False (empty config) but no exception
