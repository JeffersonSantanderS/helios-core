"""Helios v6 — Tasks module (HA-first todo tracking).

Reads Google Tasks / Todo lists from Home Assistant via the todo.get_items
service, computes context keys, and falls back to the local JSON cache if
HA is unreachable.

Context keys written:
    tasks.count              (int)  — Total task items (active + completed)
    tasks.pending            (int)  — Items with status "needs_action"
    tasks.completed_today    (int)  — Items completed today (calendar day)
    tasks.overdue            (int)  — Pending items with due date in the past
    tasks.upcoming           (int)  — Pending items with due date in the future
    tasks.source             (str)   — "home_assistant" or "json_fallback"
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

# Try to import HA client for todo items; gracefully degrade if unavailable
try:
    from ..ha_client import fetch_todo_items as _ha_fetch_todos
    HA_CLIENT_AVAILABLE = True
except ImportError:
    HA_CLIENT_AVAILABLE = False

    def _ha_fetch_todos(*args, **kwargs):  # type: ignore[misc]
        return []

from .base import BaseMod

logger = logging.getLogger("helios.tasks")

MODULE_NAME = "tasks"
SOURCE = "script_engine"

# Context UPSERT — leverages the UNIQUE(module, key, source) constraint
_CONTEXT_UPSERT_SQL = """
INSERT INTO context (source, module, key, value, priority)
VALUES (:source, :module, :key, :value, :priority)
ON CONFLICT (module, key, source) DO UPDATE SET
    value   = excluded.value,
    ts      = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    priority = excluded.priority
"""


class TasksModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "tasks",
        "version": "1.1.0",
        "description": "Tracks tasks from Home Assistant todo lists with pyicloud fallback",
        "author": "system",
        "collectors": ["tasks_state.json"],
        "dependencies": [],
        "priority": 4,
    }

    def __init__(self, db_path: str | None = None, config: dict | None = None) -> None:
        super().__init__(db_path=db_path, config=config)
        self.config = config or {}
        self._ha_enabled: bool = self.config.get("ha_enabled", True)
        self._ha_base_url: str = self.config.get(
            "ha_base_url", os.environ.get("HASS_URL", "")
        )
        self._ha_token: str = self.config.get(
            "ha_token", ""
        ) or os.environ.get("HASS_TOKEN", "") or os.environ.get("HA_TOKEN", "")
        self._ha_todos: list[str] = self.config.get("ha_todos", [])
        self._fallback_enabled: bool = self.config.get("fallback_enabled", True)
        # Default base URL from global home_assistant config if not set locally
        if self._ha_enabled and not self._ha_base_url:
            self._ha_base_url = (
                os.environ.get("HASS_URL", "")
                or os.environ.get("HOME_ASSISTANT_URL", "")
                or ""
            )

    # ------------------------------------------------------------------
    # HA Todo integration (primary)
    # ------------------------------------------------------------------

    def _fetch_ha_todos(self) -> tuple[list[dict[str, Any]], str]:
        """Fetch items from all configured HA todo entities.

        Returns:
            (items, source_label) where source_label is "home_assistant"
            only when HA is configured AND at least one item was returned.
            Returns "", even for an empty legitimate list, so that callers
            can treat "" as "HA not available/usable" and fallback if desired.
        """
        if not HA_CLIENT_AVAILABLE:
            logger.debug("HA client not available — skipping HA todo fetch")
            return [], ""
        if not self._ha_enabled:
            logger.debug("HA todo disabled — skipping")
            return [], ""
        if not self._ha_base_url or not self._ha_token:
            logger.debug("HA todo not configured (missing base_url or token)")
            return [], ""

        all_items: list[dict[str, Any]] = []
        for entity_id in self._ha_todos:
            try:
                items = _ha_fetch_todos(
                    base_url=self._ha_base_url,
                    token=self._ha_token,
                    entity_id=entity_id,
                    timeout=15,
                )
                all_items.extend(items)
            except Exception as exc:
                logger.warning("HA todo fetch failed for %s: %s", entity_id, exc)

        if all_items:
            logger.info("Fetched %d tasks from HA todo lists", len(all_items))
            return all_items, "home_assistant"
        return [], ""

    # ------------------------------------------------------------------
    # Local JSON fallback
    # ------------------------------------------------------------------

    def _read_json_state(self) -> list[dict[str, Any]]:
        """Read the local tasks_state.json file used by the legacy collector.

        The legacy format stores a flat list of tasks with "title",
        "completed", and optional "completed_at" keys.
        """
        path = os.path.expanduser("~/.hermes/helios/data/tasks_state.json")
        if not os.path.exists(path):
            return []
        try:
            with open(path) as f:
                state = json.load(f)
            return state.get("tasks", [])
        except Exception:
            return []

    def _write_json_state(self, tasks: list[dict[str, Any]], source: str) -> None:
        """Write the combined task list back to the local cache file.

        Maintains backward compatibility with any consumers of tasks_state.json.
        """
        path = os.path.expanduser("~/.hermes/helios/data/tasks_state.json")
        data = {
            "tasks": tasks,
            "ts": datetime.now(timezone.utc).isoformat(),
            "count": len(tasks),
            "pending": sum(1 for t in tasks if t.get("status") == "needs_action"),
            "completed_today": self._count_completed_today(tasks),
            "source": source,
        }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as exc:
            logger.warning("Failed to write tasks_state.json: %s", exc)

    # ------------------------------------------------------------------
    # Computed metrics
    # ------------------------------------------------------------------

    def _count_completed_today(self, tasks: list[dict[str, Any]]) -> int:
        """Count tasks whose completed_ts falls within the current calendar day."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        count = 0
        for t in tasks:
            ts = t.get("completed_ts")
            if not ts:
                # Legacy field
                ts = t.get("completed_at")
            if ts and str(ts)[:10] == today:
                count += 1
        return count

    def _compute_context(
        self, tasks: list[dict[str, Any]], source: str
    ) -> dict[str, Any]:
        """Compute all tasks context keys from the raw task list.

        Args:
            tasks: Normalized task dicts (HA or json fallback format).
            source: Data source label.

        Returns:
            Dict of context key names to computed values.
        """
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")

        pending = [t for t in tasks if t.get("status") == "needs_action"]
        completed = [t for t in tasks if t.get("status") == "completed"]

        overdue = 0
        upcoming = 0
        for t in pending:
            due = t.get("due")
            if not due:
                continue
            due_str = str(due)[:10]
            if due_str < today_str:
                overdue += 1
            elif due_str > today_str:
                upcoming += 1

        return {
            "count": len(tasks),
            "pending": len(pending),
            "completed_today": self._count_completed_today(tasks),
            "overdue": overdue,
            "upcoming": upcoming,
            "source": source,
        }

    # ------------------------------------------------------------------
    # Context persistence
    # ------------------------------------------------------------------

    def _write_context(
        self, conn: sqlite3.Connection, context_data: dict[str, Any]
    ) -> int:
        """Write computed context values to the context table.

        Returns the number of keys written.
        """
        key_map: dict[str, tuple[str, Any, int]] = {
            "count":           ("tasks.count",           context_data["count"],         0),
            "pending":         ("tasks.pending",         context_data["pending"],       0),
            "completed_today": ("tasks.completed_today", context_data["completed_today"], 0),
            "overdue":         ("tasks.overdue",         context_data["overdue"],       0),
            "upcoming":        ("tasks.upcoming",        context_data["upcoming"],      0),
            "source":          ("tasks.source",          context_data["source"],        0),
        }

        for _key, (full_key, value, priority) in key_map.items():
            json_value = json.dumps(value, default=str)
            conn.execute(
                _CONTEXT_UPSERT_SQL,
                {
                    "source": SOURCE,
                    "module": MODULE_NAME,
                    "key": full_key,
                    "value": json_value,
                    "priority": priority,
                },
            )
        return len(key_map)

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        """Main scheduler entry point. Called on each interval.

        Workflow (HA-first):
            1. Attempt to fetch Home Assistant todo items
            2. If HA returns items, normalize and use them
            3. If HA returns nothing and fallback is enabled, read local JSON
            4. Compute context keys
            5. Write context keys to context table (if db_path available)
            6. Write legacy JSON cache for backward compatibility

        Returns:
            A dict with tick results and diagnostics:
                - count (int): total tasks
                - pending (int): active tasks
                - source (str): "home_assistant" or "json_fallback"
                - fallback_used (bool): True if HA failed and JSON was used
        """
        result: dict[str, Any] = {
            "count": 0,
            "pending": 0,
            "completed_today": 0,
            "source": "none",
            "fallback_used": False,
        }

        # =====================================================================
        # Step 1 — Home Assistant (primary)
        # =====================================================================
        ha_items, ha_source = self._fetch_ha_todos()
        tasks: list[dict[str, Any]] = ha_items
        source: str = ha_source

        # =====================================================================
        # Step 2 — Fallback to local JSON cache
        # =====================================================================
        if not tasks and self._fallback_enabled:
            tasks = self._read_json_state()
            # Normalize legacy format: "title" → "summary", bool completed → status
            for t in tasks:
                if "summary" not in t and "title" in t:
                    t["summary"] = t["title"]
                if "status" not in t and "completed" in t:
                    t["status"] = "completed" if t["completed"] else "needs_action"
            if tasks:
                source = "json_fallback"
                result["fallback_used"] = True

        if tasks:
            result["source"] = source
            context_data = self._compute_context(tasks, source)
            result.update(context_data)

            # Write legacy JSON for consumers
            self._write_json_state(tasks, source)

            # Write context to DB if we have a path
            if self.db_path:
                try:
                    conn = sqlite3.connect(self.db_path)
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA foreign_keys=ON")
                    written = self._write_context(conn, context_data)
                    conn.commit()
                    conn.close()
                    result["context_written"] = written
                except Exception as exc:
                    logger.warning("Failed to write tasks context: %s", exc)

        return result
