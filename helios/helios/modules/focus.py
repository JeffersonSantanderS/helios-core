"""Helios v5 — Focus module (session tracking)."""
from .base import BaseMod
from typing import Any
import json, os, time
from datetime import datetime

class FocusModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "focus",
        "version": "1.0.0",
        "description": "Tracks active window and idle time for productivity",
        "author": "system",
        "collectors": ['focus_state.json', 'idle_state.json'],
        "dependencies": [],
        "priority": 6,
    }

    def tick(self) -> dict[str, Any]:
        path = os.path.expanduser("~/.hermes/helios/data/focus_state.json")
        data: dict[str, Any] = {"active": False, "session_minutes": 0, "sessions_today": 0, "screen_time_today_minutes": 0}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    state = json.load(f)
                mtime = os.path.getmtime(path)
                age_secs = int(time.time() - mtime)
                data["active"] = state.get("active", False)
                data["sessions_today"] = state.get("sessions_today", 0)
                data["screen_time_today_minutes"] = state.get("screen_time_today_minutes", 0)
                data["freshness_secs"] = age_secs
                data["last_updated"] = datetime.fromtimestamp(mtime).isoformat()
                if state.get("active") and state.get("start_ts"):
                    data["session_minutes"] = int((time.time() - state["start_ts"]) / 60)
                else:
                    data["session_minutes"] = state.get("last_session_minutes", 0)
            except Exception as exc:
                data["_error"] = str(exc)[:80]
        return data
