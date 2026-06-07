"""Helios v5 — Protein tracker (JSON log)."""
from .base import BaseMod
from typing import Any
import json, os
from datetime import datetime

class ProteinModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "protein",
        "version": "1.0.0",
        "description": "Tracks daily protein intake toward 160g goal",
        "author": "system",
        "collectors": [],
        "dependencies": [],
        "priority": 9,
    }

    def tick(self) -> dict[str, Any]:
        target = self.config.get("target_grams", 160) if isinstance(self.config, dict) else 160
        path = os.path.expanduser("~/.hermes/helios/data/protein_log.json")
        today = datetime.now().strftime("%Y-%m-%d")
        entries: dict = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    entries = json.load(f)
            except Exception:
                pass
        g = sum(entries.get(today, []))
        return {
            "target": target,
            "today": round(g, 1),
            "remaining": max(0, target - g),
            "pct": round(g / target * 100, 1) if target else 0.0,
        }
