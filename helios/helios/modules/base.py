"""Helios v5 - Base module class."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

log = logging.getLogger("helios.modules.base")


class BaseMod(ABC):
    """All Helios modules subclass this."""

    MODULE_MANIFEST: dict[str, Any] = {
        "name": "",
        "version": "1.0.0",
        "description": "",
        "author": "system",
        "collectors": [],
        "dependencies": [],
        "priority": 10,
    }

    def __init__(self, db_path: Optional[str] = None, config: Optional[Any] = None):
        self.db_path = db_path
        self.config = config or {}
        self.name = self.MODULE_MANIFEST.get("name") or self.__class__.__name__.lower().replace("module", "")

    @abstractmethod
    def tick(self) -> dict[str, Any]:
        """Run one data-collection pass. Return context dict."""
        raise NotImplementedError

    def health(self) -> dict[str, Any]:
        """Return health snapshot."""
        return {"status": "healthy", "name": self.name}

    def rules(self) -> list[dict[str, Any]]:
        """Declare rules this module contributes."""
        return []

    def module_info(self) -> dict[str, Any]:
        """Return module metadata for discovery and documentation."""
        return {
            **self.MODULE_MANIFEST,
            "health": self.health(),
            "rules_count": len(self.rules()),
            "enabled": self.config.get("enabled", True),
        }

    def _run_tick(self, db: Any) -> dict[str, Any]:
        """Wrapper used by engine to auto-store context."""
        result = self.tick()
        if db and isinstance(result, dict):
            for key, value in result.items():
                if key.startswith("_"):
                    continue
                prio = 0
                if isinstance(value, dict):
                    prio = value.pop("_priority", 0)
                db.set_context("script_engine", self.name, key, value, priority=prio)
        return result
