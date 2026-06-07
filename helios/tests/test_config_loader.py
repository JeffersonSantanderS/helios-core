"""Helios v7 — Test ConfigLoader dotted-key compatibility and validation."""
from pathlib import Path

import pytest

from helios.config_loader import ConfigLoader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(data: dict, path: str = "dummy") -> ConfigLoader:
    """Create a ConfigLoader with arbitrary dict data."""
    return ConfigLoader(data, Path(path))


# ---------------------------------------------------------------------------
# 1. Segmented keys (the canonical API)
# ---------------------------------------------------------------------------

class TestGetSegmentedKeys:
    """cfg.get("llm", "base_url") resolves nested dicts."""

    def test_returns_value(self):
        cfg = _cfg({"llm": {"base_url": "https://api.example.com"}})
        assert cfg.get("llm", "base_url") == "https://api.example.com"

    def test_returns_nested_dict(self):
        cfg = _cfg({"llm": {"base_url": "https://api.example.com", "model": "gpt-4"}})
        result = cfg.get("llm")
        assert result == {"base_url": "https://api.example.com", "model": "gpt-4"}

    def test_single_key(self):
        cfg = _cfg({"llm": {"base_url": "x"}})
        assert cfg.get("llm") == {"base_url": "x"}


# ---------------------------------------------------------------------------
# 2. Dotted-key backward compatibility
# ---------------------------------------------------------------------------

class TestGetDottedKeysBackwardCompat:
    """cfg.get("llm.base_url") auto-splits on '.' and resolves the same value."""

    def test_dotted_returns_same_as_segmented(self):
        data = {"llm": {"base_url": "https://api.example.com"}}
        cfg = _cfg(data)
        assert cfg.get("llm.base_url") == cfg.get("llm", "base_url")

    def test_dotted_two_levels(self):
        cfg = _cfg({"llm": {"base_url": "x"}})
        assert cfg.get("llm.base_url") == "x"

    def test_dotted_single_no_dot(self):
        """A single key without a dot should resolve as-is (no split)."""
        cfg = _cfg({"llm": {"base_url": "x"}})
        assert cfg.get("llm") == {"base_url": "x"}


# ---------------------------------------------------------------------------
# 3. Missing key returns default
# ---------------------------------------------------------------------------

class TestGetMissingKeyReturnsDefault:
    """Nonexistent keys should return the default value."""

    def test_missing_top_level(self):
        cfg = _cfg({"llm": {"base_url": "x"}})
        assert cfg.get("no_such_section", default="fallback") == "fallback"

    def test_missing_nested(self):
        cfg = _cfg({"llm": {"base_url": "x"}})
        assert cfg.get("llm", "no_such_key", default=42) == 42

    def test_missing_dotted(self):
        cfg = _cfg({"llm": {"base_url": "x"}})
        assert cfg.get("llm.nonexistent", default="nope") == "nope"

    def test_default_is_none(self):
        cfg = _cfg({})
        assert cfg.get("absent") is None

    def test_deeply_missing_intermediate(self):
        cfg = _cfg({"a": {}})
        assert cfg.get("a", "b", "c", default="end") == "end"


# ---------------------------------------------------------------------------
# 4. Deep nested segmented keys
# ---------------------------------------------------------------------------

class TestGetDeepNested:
    """cfg.get("priority", "scoring", "weights") works for 3-level nesting."""

    def test_three_levels(self):
        cfg = _cfg({"priority": {"scoring": {"weights": {"urgency": 0.5}}}})
        assert cfg.get("priority", "scoring", "weights") == {"urgency": 0.5}

    def test_three_levels_scalar(self):
        cfg = _cfg({"priority": {"scoring": {"weights": 42}}})
        assert cfg.get("priority", "scoring", "weights") == 42


# ---------------------------------------------------------------------------
# 5. Deep dotted keys
# ---------------------------------------------------------------------------

class TestGetDeepDotted:
    """cfg.get("priority.scoring.weights") resolves 3-level dotted keys."""

    def test_dotted_three_levels(self):
        data = {"priority": {"scoring": {"weights": {"urgency": 0.5}}}}
        cfg = _cfg(data)
        assert cfg.get("priority.scoring.weights") == {"urgency": 0.5}

    def test_dotted_equals_segmented(self):
        data = {"priority": {"scoring": {"weights": 7}}}
        cfg = _cfg(data)
        assert cfg.get("priority.scoring.weights") == cfg.get("priority", "scoring", "weights")


# ---------------------------------------------------------------------------
# 6. validate() — fatal on Gmail keys
# ---------------------------------------------------------------------------

class TestValidateNoGmail:
    """validate() adds a fatal entry when Gmail config keys are present."""

    def test_gmail_key_is_fatal(self):
        cfg = _cfg({"gmail_credentials": "x", "llm": {"base_url": "u"}})
        result = cfg.validate()
        assert result["valid"] is False
        assert any("Gmail" in f for f in result["fatal"])

    def test_gmail_prefix_is_fatal(self):
        cfg = _cfg({"gmail_oauth": {"creds": "abc"}, "llm": {"base_url": "u"}})
        result = cfg.validate()
        assert result["valid"] is False

    def test_no_gmail_no_fatal(self):
        cfg = _cfg({"llm": {"base_url": "u"}, "modules": {}})
        result = cfg.validate()
        assert not any("Gmail" in f for f in result["fatal"])


# ---------------------------------------------------------------------------
# 7. validate() — warnings on missing sections
# ---------------------------------------------------------------------------

class TestValidateMissingSections:
    """validate() warns or is fatal for missing recommended sections."""

    def test_missing_llm_is_fatal(self):
        cfg = _cfg({"modules": {}})
        result = cfg.validate()
        assert any("llm" in f for f in result["fatal"])

    def test_missing_modules_is_fatal(self):
        cfg = _cfg({"llm": {"base_url": "u"}})
        result = cfg.validate()
        assert any("modules" in f for f in result["fatal"])

    def test_missing_matrix_is_warning(self):
        cfg = _cfg({"llm": {"base_url": "u"}, "modules": {}})
        result = cfg.validate()
        assert any("matrix" in w for w in result["warnings"])

    def test_missing_priority_is_warning(self):
        cfg = _cfg({"llm": {"base_url": "u"}, "modules": {}})
        result = cfg.validate()
        assert any("priority" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# 8. validate_modules() — reports unconfigured / discovered modules
# ---------------------------------------------------------------------------

class TestValidateModules:
    """validate_modules() returns warnings for mismatched module entries."""

    def test_configured_but_not_discovered(self):
        """A module in config but without a corresponding .py is warned."""
        cfg = _cfg({"modules": {"fictional_module": {"enabled": True}}})
        warnings = cfg.validate_modules()
        assert any("fictional_module" in w and "configured but" in w for w in warnings)

    def test_discovered_but_not_configured(self):
        """A module .py without a config entry is warned."""
        cfg = _cfg({"modules": {}})
        warnings = cfg.validate_modules()
        # Any real module files discovered that aren't in the empty config
        # should show up as "has X.py but not in config"
        for name in warnings:
            assert "but not in config" in name or "configured but" in name


# ---------------------------------------------------------------------------
# 9. validate() — warns about missing Matrix homeserver
# ---------------------------------------------------------------------------

class TestValidateMatrixConfig:
    """validate() warns when Matrix is enabled but no homeserver is set."""

    def test_missing_homeserver_warns(self):
        cfg = _cfg({
            "llm": {"base_url": "u"},
            "modules": {},
            "matrix": {"enabled": True},
        })
        result = cfg.validate()
        assert any("homeserver" in w for w in result["warnings"])

    def test_homeserver_present_no_warning(self):
        cfg = _cfg({
            "llm": {"base_url": "u"},
            "modules": {},
            "matrix": {"enabled": True, "homeserver": "https://matrix.org"},
        })
        result = cfg.validate()
        assert not any("homeserver" in w for w in result["warnings"])

    def test_matrix_disabled_no_homeserver_warning(self):
        """When matrix is disabled, missing homeserver shouldn't warn."""
        cfg = _cfg({
            "llm": {"base_url": "u"},
            "modules": {},
            "matrix": {"enabled": False},
        })
        result = cfg.validate()
        assert not any("homeserver" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# 10. validate() — warns when no channels section
# ---------------------------------------------------------------------------

class TestValidateNoChannelsWarns:
    """validate() warns when the 'channels' section is absent or empty."""

    def test_no_channels_warns(self):
        cfg = _cfg({"llm": {"base_url": "u"}, "modules": {}})
        result = cfg.validate()
        assert any("channels" in w for w in result["warnings"])

    def test_empty_channels_warns(self):
        cfg = _cfg({"llm": {"base_url": "u"}, "modules": {}, "channels": {}})
        result = cfg.validate()
        assert any("channels" in w for w in result["warnings"])

    def test_populated_channels_no_warning(self):
        cfg = _cfg({
            "llm": {"base_url": "u"},
            "modules": {},
            "channels": {"matrix": {"room": "#test:matrix.org"}},
        })
        result = cfg.validate()
        assert not any("channels" in w for w in result["warnings"])