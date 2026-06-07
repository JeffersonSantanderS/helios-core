"""Helios v5 — Spotify module (cached state)."""
from .base import BaseMod
from typing import Any
from datetime import datetime
import json, os, time

class SpotifyModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "spotify",
        "version": "1.0.0",
        "description": "Tracks currently playing Spotify track",
        "author": "system",
        "collectors": ['spotify_state.json'],
        "dependencies": [],
        "priority": 8,
    }

    def tick(self) -> dict[str, Any]:
        data: dict[str, Any] = {"configured": False, "active": False}
        if isinstance(self.config, dict) and self.config.get("client_id"):
            data["configured"] = True
        path = os.path.expanduser("~/.hermes/helios/data/spotify_state.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    s = json.load(f)
                mtime = os.path.getmtime(path)
                age_secs = int(time.time() - mtime)
                data.update({
                    "active": s.get("is_playing", False),
                    "track": s.get("item", {}).get("name", ""),
                    "artist": ", ".join(a["name"] for a in s.get("item", {}).get("artists", [])),
                    "progress": s.get("progress_ms", 0),
                    "duration": s.get("item", {}).get("duration_ms", 0),
                    "freshness_secs": age_secs,
                    "last_updated": datetime.fromtimestamp(mtime).isoformat(),
                })
            except Exception as exc:
                data["_error"] = str(exc)[:80]
        return data
