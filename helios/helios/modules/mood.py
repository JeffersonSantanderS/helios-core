"""Helios v5 — Mood module (score tracking + Discord check-in)."""
from .base import BaseMod
from typing import Any
import json, os
from datetime import datetime

class MoodModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "mood",
        "version": "1.0.0",
        "description": "Daily mood check-ins via Matrix messages",
        "author": "system",
        "collectors": ['checkin_state.json'],
        "dependencies": [],
        "priority": 10,
    }

    def tick(self) -> dict[str, Any]:
        path = os.path.expanduser("~/.hermes/helios/data/mood_state.json")
        today = datetime.now().strftime("%Y-%m-%d")
        data: dict[str, Any] = {"score": None, "last_checkin": None, "trend": "neutral"}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    state = json.load(f)
                history = state.get("history", [])
                if history:
                    data["score"] = history[-1].get("score")
                    data["last_checkin"] = history[-1].get("date")
                    # Simple 3-day trend
                    if len(history) >= 3:
                        recent = sum(h["score"] for h in history[-3:]) / 3
                        older = sum(h["score"] for h in history[-6:-3]) / 3 if len(history) >= 6 else recent
                        if recent > older + 0.5:
                            data["trend"] = "improving"
                        elif recent < older - 0.5:
                            data["trend"] = "declining"
                        else:
                            data["trend"] = "stable"
            except Exception as exc:
                data["_error"] = str(exc)[:80]
        return data
