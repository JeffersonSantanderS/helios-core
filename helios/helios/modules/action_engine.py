"""Helios v5 — Action Engine.

Deterministic action execution triggered by rules. When the rules engine
detects a condition, it fires an action through this module. All actions
are script-engine owned — no LLM needed.

Supported actions:
  - send_matrix_nudge: Push a nudge message to Matrix via MatrixPusher
  - enable_dnd_mode: Set Do Not Disturb mode (writes context flag)
  - queue_spotify_playlist: Queue a playlist for playback
  - set_reminder: Create a timed reminder
  - adjust_schedule: Modify calendar event timing/buffer

Architecture:
  - ActionEngine.execute(action_name, params) dispatches to action handlers
  - Each handler is a deterministic function (no LLM calls)
  - Actions are logged in action_log table for audit/rollback
  - Context changes are written immediately
  - Matrix nudges go through matrix_pusher.py (imported inline)
  - Spotify actions use spotipy or direct API
  - Schedule adjustments write to calendar_events table

Context keys written:
  action_engine.dnd_enabled      (bool)  — Whether DND mode is active
  action_engine.dnd_since        (str)   — ISO timestamp when DND started
  action_engine.last_action      (str)   — Name of last executed action
  action_engine.last_action_ts   (str)   — ISO timestamp of last action
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger("helios.action_engine")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODULE_NAME = "action_engine"
SOURCE = "script_engine"

# Valid action names
VALID_ACTIONS = frozenset({
    "send_matrix_nudge",
    "enable_dnd_mode",
    "disable_dnd_mode",
    "queue_spotify_playlist",
    "set_reminder",
    "adjust_schedule",
})

# Context UPSERT SQL
_CONTEXT_UPSERT_SQL = """
INSERT INTO context (source, module, key, value, priority)
VALUES (:source, :module, :key, :value, :priority)
ON CONFLICT (module, key, source) DO UPDATE SET
    value   = excluded.value,
    ts      = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    priority = excluded.priority
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    """Return current UTC-aware datetime."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Action Handlers
# ---------------------------------------------------------------------------

def _action_send_matrix_nudge(
    engine: "ActionEngine",
    params: dict[str, Any],
) -> dict[str, Any]:
    """Send a nudge message to Matrix via MatrixPusher.

    Required params:
        message (str): The nudge message to send.
    Optional params:
        mention (str): MXID to mention (from config or env).
        urgency (str): 'low', 'medium', 'high' (prefixes message).
    """
    message = params.get("message", "")
    if not message:
        return {"success": False, "error": "Missing required param: message"}

    urgency = params.get("urgency", "low")
    urgency_prefix = {
        "high": "⚠️ **URGENT**: ",
        "medium": "ℹ️ ",
        "low": "",
    }.get(urgency, "")

    mention = params.get("mention", "")
    full_message = f"{urgency_prefix}{message}"
    if mention:
        full_message = f"{mention} {full_message}"

    # Deliver via ChannelRouter when available (Phase 3), fallback to inline MatrixPusher
    sent_via_channel = False
    urgency_priority = {"high": 2, "medium": 1, "low": 1}
    if engine.channels is not None:
        try:
            from helios.channels.events import AlertEvent
            results = engine.channels.send(AlertEvent(
                title="Matrix Nudge",
                message=full_message,
                severity="warning" if urgency == "high" else "info",
                priority=urgency_priority.get(urgency, 1),
                category="action",
                source="action_engine",
                slug="send_matrix_nudge",
            ))
            # Check if any channel succeeded
            sent_via_channel = any(r.success for r in results)
        except Exception as exc:
            logger.debug("Channel nudge failed, falling back to MatrixPusher: %s", exc)
            sent_via_channel = False

    # Fallback: inline MatrixPusher (original path, preserved for backward compat)
    if not sent_via_channel:
        try:
            from helios.matrix_pusher import MatrixPusher
            pusher = MatrixPusher(cfg=engine.config.get("matrix", {}))
            p = urgency_priority.get(urgency, 1)
            ok = pusher.push(full_message, priority=p) if p else False
        except Exception as exc:
            logger.warning("Matrix nudge failed: %s", exc)
            ok = False
    else:
        ok = True  # Delivered via channel system

    return {
        "success": ok,
        "action": "send_matrix_nudge",
        "message_sent": full_message if ok else None,
        "error": None if ok else "Matrix push failed",
    }


def _action_enable_dnd(
    engine: "ActionEngine",
    params: dict[str, Any],
) -> dict[str, Any]:
    """Enable Do Not Disturb mode.

    Optional params:
        duration_minutes (int): How long DND should last (default: 60).
        reason (str): Why DND was enabled.
    """
    now = _now_utc()
    duration = params.get("duration_minutes", 60)
    reason = params.get("reason", "Rule-triggered DND")

    # Write DND context
    conn = engine._get_conn()
    try:
        context_data = {
            "action_engine.dnd_enabled": True,
            "action_engine.dnd_since": now.isoformat(),
            "action_engine.dnd_reason": reason,
            "action_engine.dnd_duration_minutes": duration,
        }
        engine._write_context(conn, context_data)
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        return {"success": False, "action": "enable_dnd_mode", "error": str(exc)}
    finally:
        conn.close()

    return {
        "success": True,
        "action": "enable_dnd_mode",
        "dnd_enabled": True,
        "duration_minutes": duration,
        "reason": reason,
    }


def _action_disable_dnd(
    engine: "ActionEngine",
    params: dict[str, Any],
) -> dict[str, Any]:
    """Disable Do Not Disturb mode."""
    conn = engine._get_conn()
    try:
        context_data = {
            "action_engine.dnd_enabled": False,
            "action_engine.dnd_since": "",
            "action_engine.dnd_reason": "",
        }
        engine._write_context(conn, context_data)
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        return {"success": False, "action": "disable_dnd_mode", "error": str(exc)}
    finally:
        conn.close()

    return {
        "success": True,
        "action": "disable_dnd_mode",
        "dnd_enabled": False,
    }


def _action_queue_spotify_playlist(
    engine: "ActionEngine",
    params: dict[str, Any],
) -> dict[str, Any]:
    """Queue a Spotify playlist for playback.

    Required params:
        playlist_uri (str): Spotify playlist URI or ID.
    Optional params:
        device_id (str): Spotify device to play on.
        shuffle (bool): Whether to shuffle (default: True).
    """
    playlist_uri = params.get("playlist_uri", "")
    if not playlist_uri:
        return {"success": False, "error": "Missing required param: playlist_uri"}

    device_id = params.get("device_id", "")
    shuffle = params.get("shuffle", True)

    # Try to use spotipy if available
    try:
        import spotipy  # type: ignore[import-untyped]

        # Build spotipy instance from config
        sp_config = engine.config.get("spotify", {})
        if sp_config:
            auth_manager = spotipy.SpotifyOAuth(
                client_id=sp_config.get("client_id", ""),
                client_secret=sp_config.get("client_secret", ""),
                redirect_uri=sp_config.get("redirect_uri", "http://localhost"),
                scope="user-modify-playback-state",
            )
            sp = spotipy.Spotify(auth_manager=auth_manager)
            kwargs = {"uris": [playlist_uri]}
            if device_id:
                kwargs["device_id"] = device_id

            sp.start_playback(**kwargs)

            return {
                "success": True,
                "action": "queue_spotify_playlist",
                "playlist_uri": playlist_uri,
                "device_id": device_id,
                "shuffle": shuffle,
            }
    except ImportError:
        logger.debug("spotipy not installed — Spotify action logged but not executed")
    except Exception as exc:
        logger.warning("Spotify action failed: %s", exc)
        return {
            "success": False,
            "action": "queue_spotify_playlist",
            "error": str(exc),
        }

    # No spotipy — just log the action
    return {
        "success": True,
        "action": "queue_spotify_playlist",
        "playlist_uri": playlist_uri,
        "device_id": device_id,
        "shuffle": shuffle,
        "note": "Logged only — spotipy not available",
    }


def _action_set_reminder(
    engine: "ActionEngine",
    params: dict[str, Any],
) -> dict[str, Any]:
    """Create a timed reminder.

    Required params:
        text (str): Reminder text.
        remind_at (str): ISO8601 datetime when to remind.
    Optional params:
        priority (str): 'low', 'medium', 'high' (default: 'medium').
    """
    text = params.get("text", "")
    remind_at_str = params.get("remind_at", "")

    if not text:
        return {"success": False, "error": "Missing required param: text"}
    if not remind_at_str:
        return {"success": False, "error": "Missing required param: remind_at"}

    priority = params.get("priority", "medium")

    conn = engine._get_conn()
    try:
        conn.execute(
            """
            INSERT INTO reminders (text, priority, remind_at, completed, source)
            VALUES (?, ?, ?, 0, 'script_engine')
            """,
            (text, priority, remind_at_str),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        return {"success": False, "action": "set_reminder", "error": str(exc)}
    finally:
        conn.close()

    return {
        "success": True,
        "action": "set_reminder",
        "text": text,
        "remind_at": remind_at_str,
        "priority": priority,
    }


def _action_adjust_schedule(
    engine: "ActionEngine",
    params: dict[str, Any],
) -> dict[str, Any]:
    """Adjust a calendar event (shift time, add buffer, etc.).

    Required params:
        event_id (str): icloud_id of the event to adjust.
    Optional params:
        shift_minutes (int): Minutes to shift start/end time.
        add_buffer_minutes (int): Add free time after event.
        new_title (str): Rename the event.
        new_busy_free (str): Change busy/free status.
    """
    event_id = params.get("event_id", "")
    if not event_id:
        return {"success": False, "error": "Missing required param: event_id"}

    conn = engine._get_conn()
    try:
        # Fetch current event
        row = conn.execute(
            "SELECT * FROM calendar_events WHERE icloud_id = ?",
            (event_id,),
        ).fetchone()

        if not row:
            return {"success": False, "action": "adjust_schedule",
                    "error": f"Event not found: {event_id}"}

        # Build updates
        updates = []
        update_values = []

        shift = params.get("shift_minutes", 0)
        if shift:
            from helios.modules.calendar import _parse_iso8601

            start_dt_str = row["start_time"]
            end_dt_str = row["end_time"]
            start_dt = _parse_iso8601(start_dt_str) + timedelta(minutes=shift)
            end_dt = _parse_iso8601(end_dt_str) + timedelta(minutes=shift)
            updates.extend(["start_time = ?", "end_time = ?"])
            update_values.extend([start_dt.isoformat(), end_dt.isoformat()])

        buffer = params.get("add_buffer_minutes", 0)
        if buffer:
            from helios.modules.calendar import _parse_iso8601

            end_dt_str = row["end_time"]
            end_dt = _parse_iso8601(end_dt_str) + timedelta(minutes=buffer)
            updates.append("end_time = ?")
            update_values.append(end_dt.isoformat())

        new_title = params.get("new_title")
        if new_title:
            updates.append("title = ?")
            update_values.append(new_title)

        new_busy_free = params.get("new_busy_free")
        if new_busy_free:
            updates.append("busy_free = ?")
            update_values.append(new_busy_free)

        if not updates:
            return {"success": True, "action": "adjust_schedule",
                    "note": "No changes specified", "event_id": event_id}

        # Apply updates
        update_values.append(event_id)
        sql = f"UPDATE calendar_events SET {', '.join(updates)} WHERE icloud_id = ?"
        conn.execute(sql, update_values)
        conn.commit()

        return {
            "success": True,
            "action": "adjust_schedule",
            "event_id": event_id,
            "changes": updates,
        }

    except sqlite3.Error as exc:
        conn.rollback()
        return {"success": False, "action": "adjust_schedule", "error": str(exc)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ActionEngine Class
# ---------------------------------------------------------------------------

class ActionEngine:
    """Deterministic action executor.

    Provides stateful access to context and action handlers.
    Usage:
        engine = ActionEngine(db_path, config)
        result = engine.execute("send_matrix_nudge", {"message": "Time to hydrate!"})
    """

    _ACTION_MAP: dict[str, Callable[["ActionEngine", dict[str, Any]], dict[str, Any]]] = {
        "send_matrix_nudge": _action_send_matrix_nudge,
        "enable_dnd_mode": _action_enable_dnd,
        "disable_dnd_mode": _action_disable_dnd,
        "queue_spotify_playlist": _action_queue_spotify_playlist,
        "set_reminder": _action_set_reminder,
        "adjust_schedule": _action_adjust_schedule,
    }

    def __init__(self, db_path: str, config: dict | None = None) -> None:
        self.db_path = db_path
        self.config = config or {}
        self.channels: Optional["ChannelRouter"] = None  # Set by HeliosEngine after init; Phase 3 channel routing
        self._ensure_tables()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a new SQLite connection with WAL mode and row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_tables(self) -> None:
        """Create action-related tables if they don't exist."""
        try:
            conn = self._get_conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS action_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    action      TEXT NOT NULL,
                    params      TEXT NOT NULL DEFAULT '{}',
                    result      TEXT NOT NULL DEFAULT '{}',
                    success     INTEGER NOT NULL DEFAULT 0,
                    source      TEXT NOT NULL DEFAULT 'script_engine',
                    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                CREATE INDEX IF NOT EXISTS idx_action_log_ts ON action_log (ts);
                CREATE INDEX IF NOT EXISTS idx_action_log_action ON action_log (action);
            """)
            conn.close()
        except sqlite3.Error as exc:
            logger.error("Failed to ensure action_log table: %s", exc)

    def _write_context(self, conn: sqlite3.Connection, context_data: dict[str, Any]) -> None:
        """Write context values to the context table."""
        for full_key, value in context_data.items():
            json_value = json.dumps(value, default=str)
            conn.execute(
                _CONTEXT_UPSERT_SQL,
                {
                    "source": SOURCE,
                    "module": MODULE_NAME,
                    "key": full_key,
                    "value": json_value,
                    "priority": 0,
                },
            )

    def execute(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute an action by name with the given parameters.

        Args:
            action: Name of the action to execute (must be in VALID_ACTIONS).
            params: Dict of action parameters.

        Returns:
            Result dict with at least 'success' (bool), 'action' (str),
            and 'error' (str|None). Additional keys vary by action type.
        """
        if params is None:
            params = {}

        if action not in VALID_ACTIONS:
            logger.warning("Unknown action: %s", action)
            return {
                "success": False,
                "action": action,
                "error": f"Unknown action: {action}",
            }

        handler = self._ACTION_MAP[action]
        logger.info("Executing action: %s", action)

        try:
            result = handler(self, params)
        except Exception as exc:
            logger.exception("Action %s failed", action)
            result = {
                "success": False,
                "action": action,
                "error": str(exc),
                "params": params,
            }

        # Log the action
        try:
            conn = self._get_conn()
            conn.execute("""
                INSERT INTO action_log (action, params, result, success, source)
                VALUES (?, ?, ?, ?, ?)
            """, (action, json.dumps(params, default=str), json.dumps(result, default=str),
                  1 if result.get("success") else 0, "action_engine"))
            conn.commit()
            conn.close()
        except sqlite3.Error as exc:
            logger.error("Failed to log action: %s", exc)

        return result

    def is_dnd_enabled(self) -> bool:
        """Read DND state from context table."""
        try:
            conn = self._get_conn()
            row = conn.execute("""
                SELECT value FROM context
                WHERE source = ? AND module = ? AND key = ?
                ORDER BY ts DESC LIMIT 1
            """, (SOURCE, MODULE_NAME, "action_engine.dnd_enabled")).fetchone()
            conn.close()
            if row:
                val = json.loads(row[0])
                return bool(val)
        except Exception:
            pass
        return False

    def get_pending_reminders(self) -> list[dict[str, Any]]:
        """Return reminders that are due or overdue."""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT text, remind_at, priority, created_at
                FROM reminders
                WHERE DATETIME(remind_at) <= ? AND completed = 0
                ORDER BY remind_at ASC
            """, (_now_utc().isoformat(),)).fetchall()
            conn.close()
            return [
                {
                    "text": r[0],
                    "remind_at": r[1],
                    "priority": r[2],
                    "created_at": r[3],
                }
                for r in rows
            ]
        except sqlite3.Error:
            conn.close()
            return []

    def get_available_actions(self) -> list[str]:
        """Return list of available action names."""
        return sorted(VALID_ACTIONS)

    def status(self) -> dict[str, Any]:
        """Return module status for health checks."""
        return {
            "module": MODULE_NAME,
            "available_actions": sorted(VALID_ACTIONS),
            "matrix_push_available": True,
            "dnd_enabled": self.is_dnd_enabled(),
            "db_path": self.db_path,
        }
