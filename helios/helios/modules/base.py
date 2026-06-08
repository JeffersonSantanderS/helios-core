"""Helios v6 - Base module class with optional encrypted state at rest."""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("helios.modules.base")


class BaseMod(ABC):
    """All Helios modules subclass this."""

    # Subclasses that store PII should set encrypted_state = True to get
    # automatic Fernet encryption of their JSON state files at rest.
    encrypted_state: bool = False

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

    # ── Encrypted state helpers ─────────────────────────────────────

    @staticmethod
    def _state_dir() -> Path:
        """Return the data directory for module state files."""
        base = os.environ.get(
            "HELIOS_BASE",
            os.path.join(os.path.expanduser("~"), ".hermes", "helios"),
        )
        return Path(base) / "data"

    def _state_path(self, filename: str) -> Path:
        """Return the plaintext path for a state file."""
        return self._state_dir() / filename

    def _encrypted_state_path(self, filename: str) -> Path:
        """Return the encrypted path for a state file (.enc suffix)."""
        return self._state_dir() / f"{filename}.enc"

    def _save_state_encrypted(self, filename: str, data: dict[str, Any]) -> Path:
        """Encrypt and save module state. Returns the encrypted file path.

        If ``encrypted_state`` is False the data is written as regular JSON.
        """
        state_dir = self._state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)

        if not self.encrypted_state:
            path = self._state_path(filename)
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            return path

        # Encrypted path
        enc_path = self._encrypted_state_path(filename)
        try:
            from helios.crypto import encrypt_data
            token = encrypt_data(data)
            enc_path.write_bytes(token)
            log.debug("Saved encrypted state → %s", enc_path)
            return enc_path
        except Exception as exc:
            log.warning("Failed to encrypt state for %s (%s), falling back to plaintext", filename, exc)
            # Fallback: write plaintext so the module never loses data
            path = self._state_path(filename)
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            return path

    def _load_state_encrypted(self, filename: str) -> dict[str, Any]:
        """Load module state, handling encrypted, plaintext, and migration.

        Resolution order:
          1. Encrypted file (.enc) — decrypt and return.
          2. Plaintext JSON file — load normally, then re-save encrypted
             (seamless migration) and delete the old plaintext file.
          3. Neither file exists — return empty dict.

        If the key is missing or the encrypted file is corrupted, log a
        warning and return an empty dict (never crash the daemon).
        """
        enc_path = self._encrypted_state_path(filename)
        plain_path = self._state_path(filename)

        # 1. Prefer encrypted file
        if enc_path.exists():
            try:
                from helios.crypto import decrypt_data
                result = decrypt_data(enc_path.read_bytes())
                if not isinstance(result, dict):
                    log.warning("Decrypted state for %s is not a dict, resetting", filename)
                    return {}
                return result
            except Exception as exc:
                log.warning("Failed to decrypt %s: %s — returning empty state", enc_path, exc)
                return {}

        # 2. Plaintext file — migrate if encrypted_state is True
        if plain_path.exists():
            try:
                data = json.loads(plain_path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Failed to read plaintext state %s: %s", plain_path, exc)
                return {}

            if self.encrypted_state and isinstance(data, dict):
                log.info("Migrating plaintext → encrypted state for %s", filename)
                saved = self._save_state_encrypted(filename, data)
                # Only delete plaintext after successful encrypted save
                if saved.suffix == ".enc":
                    try:
                        plain_path.unlink()
                        log.info("Removed plaintext state after migration: %s", plain_path)
                    except OSError as exc:
                        log.warning("Could not remove plaintext %s: %s", plain_path, exc)
            return data if isinstance(data, dict) else {}

        # 3. No state file found
        return {}
