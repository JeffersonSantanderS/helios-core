"""Helios v5 — Habits module (streak tracking)."""
from .base import BaseMod
from typing import Any
import json, os
from datetime import datetime

class HabitsModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "habits",
        "version": "1.0.0",
        "description": "Tracks habit streaks (gym, reading, protein)",
        "author": "system",
        "collectors": [],
        "dependencies": [],
        "priority": 11,
    }

    def tick(self) -> dict[str, Any]:
        path = os.path.expanduser("~/.hermes/helios/data/habits_state.json")
        today = datetime.now().strftime("%Y-%m-%d")
        data: dict[str, Any] = {"habits": [], "completed_today": 0, "total": 0}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    state = json.load(f)
                habits = state.get("habits", [])
                data["habits"] = habits
                data["total"] = len(habits)
                data["completed_today"] = sum(1 for h in habits if h.get("last_completed") == today)
            except Exception as exc:
                data["_error"] = str(exc)[:80]
        return data
