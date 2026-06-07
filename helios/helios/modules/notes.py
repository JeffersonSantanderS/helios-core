"""Helios v5 — Notes module (Obsidian vault sync)."""
from .base import BaseMod
from typing import Any
import json, os

class NotesModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "notes",
        "version": "1.0.0",
        "description": "Syncs iCloud notes",
        "author": "system",
        "collectors": ['notes_cache.json'],
        "dependencies": ['pyicloud'],
        "priority": 6,
    }

    def _vault_path(self) -> str:
        return os.path.expanduser("~/.hermes/helios/data/obsidian_vault")

    def tick(self) -> dict[str, Any]:
        vault = self._vault_path()
        path = os.path.expanduser("~/.hermes/helios/data/notes_state.json")
        data: dict[str, Any] = {"notes_count": 0, "latest_note": None, "vault_exists": False}
        if os.path.isdir(vault):
            data["vault_exists"] = True
            try:
                md_files = [f for f in os.listdir(vault) if f.endswith(".md")]
                data["notes_count"] = len(md_files)
                if md_files:
                    latest = max(md_files, key=lambda f: os.path.getmtime(os.path.join(vault, f)))
                    data["latest_note"] = latest
            except Exception as exc:
                data["_error"] = str(exc)[:80]
        if os.path.exists(path):
            try:
                with open(path) as f:
                    state = json.load(f)
                if not data["notes_count"]:
                    data["notes_count"] = state.get("count", 0)
            except Exception:
                pass
        return data
