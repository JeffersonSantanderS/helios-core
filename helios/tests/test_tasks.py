"""Tests for the Tasks module (HA-first todo tracking)."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Any

import pytest

import helios.modules.tasks as tasks_mod
from helios.modules.tasks import TasksModule


@pytest.fixture
def fresh_db():
    """Provide a temporary SQLite DB with the context table ready."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = sqlite3.connect(path)
    conn.execute("""
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
    """)
    conn.commit()
    conn.close()
    yield path
    os.remove(path)


@pytest.fixture
def config_with_ha():
    """Module config wired to the local HA test env."""
    return {
        "enabled": True,
        "ha_enabled": True,
        "ha_base_url": "http://homeassistant.local:8123",
        "ha_todos": ["todo.my_tasks"],
        "fallback_enabled": True,
    }


@pytest.fixture
def config_no_ha():
    """Module config with HA disabled, fallback only."""
    return {
        "enabled": True,
        "ha_enabled": False,
        "fallback_enabled": True,
    }


class TestTasksModuleCompute:
    """Pure logic: _compute_context and _count_completed_today."""

    def test_compute_empty(self):
        mod = TasksModule(db_path=":memory:", config={})
        ctx = mod._compute_context([], "none")
        assert ctx["count"] == 0
        assert ctx["pending"] == 0
        assert ctx["completed_today"] == 0
        assert ctx["overdue"] == 0
        assert ctx["upcoming"] == 0
        assert ctx["source"] == "none"

    def test_compute_active_and_completed(self):
        mod = TasksModule(db_path=":memory:", config={})
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tasks = [
            {"summary": "A", "status": "needs_action", "due": today},
            {"summary": "B", "status": "completed", "completed_ts": f"{today}T12:00:00Z"},
            {"summary": "C", "status": "needs_action"},
        ]
        ctx = mod._compute_context(tasks, "home_assistant")
        assert ctx["count"] == 3
        assert ctx["pending"] == 2
        assert ctx["completed_today"] == 1

    def test_overdue_and_upcoming(self):
        mod = TasksModule(db_path=":memory:", config={})
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        tasks = [
            {"summary": "Past",   "status": "needs_action", "due": yesterday},
            {"summary": "Today",  "status": "needs_action", "due": today},
            {"summary": "Future", "status": "needs_action", "due": tomorrow},
        ]
        ctx = mod._compute_context(tasks, "home_assistant")
        assert ctx["overdue"] == 1
        assert ctx["upcoming"] == 1

    def test_count_completed_today_legacy(self):
        mod = TasksModule(db_path=":memory:", config={})
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tasks = [
            {"summary": "A", "completed": True, "completed_at": f"{today}T08:00:00Z"},
            {"summary": "B", "completed": True, "completed_at": f"{today}T14:00:00Z"},
            {"summary": "C", "completed": True, "completed_at": f"2000-01-01T00:00:00Z"},
        ]
        assert mod._count_completed_today(tasks) == 2


class TestTasksModuleTick:
    """Integration-style tick() tests."""

    def test_tick_fallback_to_json(self, fresh_db, config_no_ha):
        """When HA is disabled, tick reads the legacy JSON cache."""
        state_path = os.path.expanduser("~/.hermes/helios/data/tasks_state.json")
        old_exists = os.path.exists(state_path)
        old_backup = None
        if old_exists:
            with open(state_path) as f:
                old_backup = f.read()

        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        sample = {
            "tasks": [
                {"title": "Buy milk", "completed": False, "completed_at": ""},
                {"title": "Email boss", "completed": True, "completed_at": f"{today}T10:00:00Z"},
            ]
        }
        try:
            with open(state_path, "w") as f:
                json.dump(sample, f)

            mod = TasksModule(db_path=fresh_db, config=config_no_ha)
            result = mod.tick()

            assert result["count"] == 2
            assert result["pending"] == 1
            assert result["completed_today"] == 1
            assert result["source"] == "json_fallback"
            assert result["fallback_used"] is True
        finally:
            if old_backup is not None:
                with open(state_path, "w") as f:
                    f.write(old_backup)
            elif not old_exists:
                os.remove(state_path)

    def test_tick_empty_no_fallback(self, fresh_db):
        """When no data is available at all, return zeros."""
        config = {"enabled": True, "ha_enabled": False, "fallback_enabled": False}
        mod = TasksModule(db_path=fresh_db, config=config)
        result = mod.tick()
        assert result["count"] == 0
        assert result["source"] == "none"

    def test_context_persisted_to_db(self, fresh_db):
        """Tick writes context keys to the database when db_path is available."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state_path = os.path.expanduser("~/.hermes/helios/data/tasks_state.json")
        old_exists = os.path.exists(state_path)
        old_backup = None
        if old_exists:
            with open(state_path) as f:
                old_backup = f.read()

        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        sample = {
            "tasks": [
                {"title": "Task 1", "completed": True, "completed_at": f"{today}T09:00:00Z"},
                {"title": "Task 2", "completed": False},
            ]
        }
        try:
            with open(state_path, "w") as f:
                json.dump(sample, f)

            config = {"enabled": True, "ha_enabled": False, "fallback_enabled": True}
            mod = TasksModule(db_path=fresh_db, config=config)
            result = mod.tick()

            assert result.get("context_written", 0) > 0
            conn = sqlite3.connect(fresh_db)
            rows = conn.execute(
                "SELECT key, value FROM context WHERE module = 'tasks'"
            ).fetchall()
            conn.close()
            keys = {r[0] for r in rows}
            assert "tasks.count" in keys
            assert "tasks.pending" in keys
            assert "tasks.source" in keys
        finally:
            if old_backup is not None:
                with open(state_path, "w") as f:
                    f.write(old_backup)
            elif not old_exists:
                os.remove(state_path)


class TestTasksModuleRealHA:
    """Live HA network tests — guarded by token availability."""

    def _has_token(self) -> bool:
        token = os.environ.get("HASS_TOKEN") or os.environ.get("HA_TOKEN", "")
        return bool(token)

    @pytest.mark.skipif(not os.environ.get("HASS_TOKEN") and not os.environ.get("HA_TOKEN"),
                        reason="No HASS_TOKEN/HA_TOKEN in env")
    def test_fetch_ha_todos_live(self, config_with_ha):
        """Live test: fetch actual todos from HA.

        Should not raise regardless of token validity or list emptiness.
        """
        mod = TasksModule(config=config_with_ha)
        items, src = mod._fetch_ha_todos()
        assert isinstance(items, list)
        # When HA unreachable or returns empty, src may be "".  When it
        # returns items, src is "home_assistant".  Both are acceptable.
        assert src in ("home_assistant", "")


class TestTasksModuleManifest:
    def test_module_info(self):
        mod = TasksModule()
        info = mod.module_info()
        assert info["name"] == "tasks"
        assert info["version"] == "1.1.0"
        assert "description" in info
