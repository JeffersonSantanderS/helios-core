"""Helios v6 — Configuration loader.

Looks for config in this order:
  1. ./helios/config/config.yaml  (repo clone)
  2. ~/.hermes/helios/config/config.yaml  (user override)
  3. Env: HELIOS_CONFIG

Supports both segmented keys (cfg.get("llm", "base_url"))
and dotted keys (cfg.get("llm.base_url")) for backward compatibility.
"""
from __future__ import annotations
import os
import yaml
from pathlib import Path
from typing import Any


class ConfigLoader:
    """Minimal YAML config loader with dotted-key compatibility."""

    @classmethod
    def load(cls) -> "ConfigLoader":
        path = cls._find_config()
        raw = cls._load_yaml(path)
        return cls(raw, path)

    def __init__(self, data: dict[str, Any], path: Path) -> None:
        self._data = data
        self._path = path

    def get(self, *keys: str, default: Any = None) -> Any:
        """Get a nested config value.

        Supports two calling patterns:
          - Segmented: cfg.get("llm", "base_url") — traverse nested dicts
          - Dotted:    cfg.get("llm.base_url") — split on '.' then traverse

        If a single key contains '.', it is split on '.' for backward
        compatibility with callers that used dotted keys.
        """
        # Dotted-key compatibility: if a single key contains '.', split it
        if len(keys) == 1 and isinstance(keys[0], str) and '.' in keys[0]:
            keys = tuple(keys[0].split('.'))

        d = self._data
        for k in keys:
            if isinstance(d, dict) and k in d:
                d = d[k]
            else:
                return default
        return d

    @property
    def modules(self) -> dict[str, dict]:
        return self._data.get("modules", {})

    def validate_modules(self) -> list[str]:
        """Validate module config against discovered implementations.

        Returns list of warnings (empty = all good).
        """
        import pkgutil
        from pathlib import Path as _P

        modules_dir = _P(__file__).parent / "modules"
        available = set()
        for finder, mod_name, is_pkg in pkgutil.iter_modules([str(modules_dir)]):
            if not mod_name.startswith("_") and mod_name not in ("base", "__init__", "action_engine"):
                available.add(mod_name)

        configured = set(self.modules.keys())
        warnings = []

        for name in configured - available:
            warnings.append(f"Module '{name}' configured but no {name}.py found")

        for name in available - configured:
            warnings.append(f"Module '{name}' has {name}.py but not in config")

        return warnings

    def validate(self) -> dict[str, Any]:
        """Run config validation and return structured results.

        Returns dict with:
          - 'valid': bool — True if no fatal issues
          - 'fatal': list[str] — issues that make the config unusable
          - 'warnings': list[str] — non-fatal issues
          - 'config_path': str — resolved config path
        """
        fatal: list[str] = []
        warnings: list[str] = []

        # Module validation
        mod_warnings = self.validate_modules()
        warnings.extend(mod_warnings)

        # Check Gmail boundary — no raw Gmail config should be present
        gmail_keys = [k for k in self._data if k.startswith("gmail")]
        if gmail_keys:
            fatal.append(f"Gmail config keys found: {gmail_keys}. Raw Gmail integration is forbidden.")

        # Check Matrix config can resolve homeserver/room
        matrix_cfg = self._data.get("matrix", {})
        if matrix_cfg.get("enabled", True):
            if not matrix_cfg.get("homeserver"):
                warnings.append("Matrix enabled but no homeserver configured")
            # Token check — should not print token
            token = matrix_cfg.get("access_token", "")
            if token and len(token) > 5:
                # Token exists, just confirm it's not hardcoded empty
                pass

        # Check recommended sections
        for section in ["llm", "matrix", "modules", "priority"]:
            if section not in self._data:
                if section in ("llm", "modules"):
                    fatal.append(f"Missing recommended section: {section}")
                else:
                    warnings.append(f"Missing recommended section: {section}")

        # Check channels section
        channels = self._data.get("channels", {})
        if not channels:
            warnings.append("No 'channels' section — falling back to legacy single-channel config")

        return {
            "valid": len(fatal) == 0,
            "fatal": fatal,
            "warnings": warnings,
            "config_path": str(self._path),
        }

    @classmethod
    def _find_config(cls) -> Path:
        env = os.getenv("HELIOS_CONFIG")
        if env:
            return Path(env)
        candidates = [
            Path.cwd() / "config" / "config.yaml",
            Path.cwd() / "helios" / "config" / "config.yaml",
            Path.home() / ".hermes" / "helios-v6" / "helios" / "config" / "config.yaml",
            Path.home() / ".hermes" / "helios" / "config" / "config.yaml",
        ]
        for c in candidates:
            if c.exists():
                return c
        fallback = candidates[1]
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback

    @classmethod
    def _load_yaml(cls, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with open(path) as f:
            return yaml.safe_load(f) or {}