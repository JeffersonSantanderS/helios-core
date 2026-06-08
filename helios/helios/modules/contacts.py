"""Helios v5 — Contacts module (JSON state)."""
from .base import BaseMod
from typing import Any
import json, os

class ContactsModule(BaseMod):
    encrypted_state = True  # Personal contacts are PII

    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "contacts",
        "version": "1.0.0",
        "description": "Syncs iCloud contacts",
        "author": "system",
        "collectors": ['contacts_cache.json'],
        "dependencies": ['pyicloud'],
        "priority": 4,
    }

    def tick(self) -> dict[str, Any]:
        path = os.path.expanduser("~/.hermes/helios/data/contacts_state.json")
        data: dict[str, Any] = {"contacts": [], "count": 0}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    state = json.load(f)
                data["contacts"] = state.get("contacts", [])
                data["count"] = len(data["contacts"])
            except Exception as exc:
                data["_error"] = str(exc)[:80]
        return data
