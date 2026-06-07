"""Helios v5 — Reminders module (iCloud / JSON sync)."""
from .base import BaseMod
from typing import Any
import json, os
from datetime import datetime

class RemindersModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "reminders",
        "version": "1.0.0",
        "description": "Syncs iCloud reminders",
        "author": "system",
        "collectors": ['reminders_cache.json'],
        "dependencies": ['pyicloud'],
        "priority": 3,
    }

    def tick(self) -> dict[str, Any]:
        path = os.path.expanduser("~/.hermes/helios/data/reminders_state.json")
        now = datetime.now()
        data: dict[str, Any] = {"count": 0, "overdue": 0, "upcoming": 0, "reminders": []}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    state = json.load(f)
                reminders = state.get("reminders", [])
                data["reminders"] = reminders
                data["count"] = len(reminders)
                data["overdue"] = sum(1 for r in reminders if r.get("due") and r["due"] < now.isoformat() and not r.get("completed"))
                data["upcoming"] = sum(1 for r in reminders if r.get("due") and r["due"] >= now.isoformat() and not r.get("completed"))
            except Exception as exc:
                data["_error"] = str(exc)[:80]
        return data
