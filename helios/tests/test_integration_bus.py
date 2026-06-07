"""Helios v5 — Integration Bus Tests.

Tests for helios.integration_bus module — HA state snapshot bus.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from helios.integration_bus import HAIntegrationBus, STATE_FILE, _get_token, _compute_freshness

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MOCK_BASE_URL = "http://homeassistant.local:8123"
MOCK_TOKEN = "test-ha-token"

MOCK_ALL_STATES = [
    {
        "entity_id": "device_tracker.user_iphone",
        "state": "home",
        "attributes": {
            "latitude": 40.7128,
            "longitude": -74.0060,
            "gps_accuracy": 10,
            "friendly_name": "User iPhone",
        },
        "last_updated": (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
    },
    {
        "entity_id": "sensor.temperature",
        "state": "22.5",
        "attributes": {"unit_of_measurement": "°C"},
        "last_updated": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    },
    {
        "entity_id": "calendar.jefferson",
        "state": "on",
        "attributes": {},
        "last_updated": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
    },
]

MINIMAL_CONFIG = {
    "home_assistant": {
        "enabled": True,
        "base_url": MOCK_BASE_URL,
        "token_env": "HASS_TOKEN",
        "timeout": 15,
        "sensors": {
            "include_domains": ["sensor", "device_tracker", "calendar"],
        },
    },
}


@pytest.fixture()
def bus():
    """Return an HAIntegrationBus with test config."""
    return HAIntegrationBus(MINIMAL_CONFIG)


# ===================================================================
# 0. Token loading
# ===================================================================

class TestGetToken:
    def test_token_from_env_key(self):
        """Loads token from the config-specified env var."""
        ha_cfg = {"token_env": "HASS_TOKEN"}
        with patch.dict(os.environ, {"HASS_TOKEN": "abc123"}, clear=False):
            assert _get_token(ha_cfg) == "abc123"

    def test_fallback_hass_token(self):
        """Falls back to HASS_TOKEN when no token_env specified."""
        ha_cfg = {}
        with patch.dict(os.environ, {"HASS_TOKEN": "fallback"}, clear=False):
            assert _get_token(ha_cfg) == "fallback"

    def test_no_token(self):
        """Returns empty string when no token env vars set."""
        ha_cfg = {}
        with patch.dict(os.environ, {}, clear=True):
            # Clear any HA tokens that might be in real env
            env = {k: v for k, v in os.environ.items() if k not in ("HASS_TOKEN", "HA_TOKEN")}
            with patch.dict(os.environ, env, clear=True):
                assert _get_token(ha_cfg) == ""


# ===================================================================
# 1. Snapshot basic
# ===================================================================

class TestSnapshot:
    def test_disabled_returns_disabled(self):
        """When HA is disabled, snapshot returns state=disabled."""
        config = {"home_assistant": {"enabled": False}}
        bus = HAIntegrationBus(config)
        result = bus.snapshot()
        assert result["state"] == "disabled"

    @patch("helios.integration_bus.ha_client.fetch_all_states")
    @patch.dict(os.environ, {"HASS_TOKEN": MOCK_TOKEN})
    def test_working_ha(self, mock_fetch, bus, tmp_path):
        """Working HA returns snapshot with entities."""
        mock_fetch.return_value = MOCK_ALL_STATES

        # Override state file path
        with patch.object(bus, "_atomic_write_json") as mock_write:
            result = bus.snapshot()

        assert result["source"] == "home_assistant"
        assert result["state"] == "healthy"
        assert len(result["entities"]) == 3  # All 3 match include_domains
        assert "device_tracker.user_iphone" in result["entities"]

    @patch("helios.integration_bus.ha_client.fetch_all_states", return_value=[])
    @patch.dict(os.environ, {"HASS_TOKEN": MOCK_TOKEN})
    def test_unavailable_ha(self, mock_fetch, bus):
        """Unavailable HA (empty response) returns state=failed."""
        result = bus.snapshot()
        assert result["state"] == "failed"

    def test_no_token(self, bus):
        """Missing token returns state=failed."""
        with patch.dict(os.environ, {}, clear=True):
            env = {k: v for k, v in os.environ.items() if k not in ("HASS_TOKEN", "HA_TOKEN")}
            with patch.dict(os.environ, env, clear=True):
                result = bus.snapshot()
        assert result["state"] == "failed"


# ===================================================================
# 2. Should include
# ===================================================================

class TestShouldInclude:
    def test_matching_domain(self, bus):
        """Includes entities from configured domains."""
        assert bus._should_include("sensor.temperature") is True
        assert bus._should_include("device_tracker.phone") is True
        assert bus._should_include("calendar.work") is True

    def test_excluded_domain(self, bus):
        """Excludes entities not in include_domains."""
        assert bus._should_include("light.lamp") is False
        assert bus._should_include("switch.plug") is False

    def test_no_include_domains(self):
        """When include_domains is empty, includes everything."""
        config = {"home_assistant": {"enabled": True, "base_url": MOCK_BASE_URL, "sensors": {"include_domains": []}}}
        bus = HAIntegrationBus(config)
        assert bus._should_include("anything.goes") is True


# ===================================================================
# 3. Confidence computation
# ===================================================================

class TestConfidence:
    def test_fresh_data(self, bus):
        """Fresh data (<60s) gets confidence 1.0."""
        assert bus._compute_confidence(30) == 1.0
        assert bus._compute_confidence(0) == 1.0

    def test_moderately_stale(self, bus):
        """Moderately stale data gets reduced confidence."""
        # 1 hour old: should be between 0 and 1
        conf = bus._compute_confidence(3600)
        assert 0.0 < conf < 1.0

    def test_very_stale(self, bus):
        """Very stale data (>12h) gets confidence 0.0."""
        assert bus._compute_confidence(12 * 3600) == 0.0
        assert bus._compute_confidence(24 * 3600) == 0.0


# ===================================================================
# 4. Freshness computation
# ===================================================================

class TestFreshness:
    def test_recent_entity(self):
        """Recent entity has small freshness_seconds."""
        entity = {"last_updated": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()}
        freshness = _compute_freshness(entity)
        assert freshness is not None
        assert freshness < 15  # allow a bit of slack

    def test_missing_timestamp(self):
        """Entity without last_updated returns None."""
        assert _compute_freshness({}) is None

    def test_invalid_timestamp(self):
        """Entity with unparseable timestamp returns None."""
        assert _compute_freshness({"last_updated": "not-a-date"}) is None


# ===================================================================
# 5. Atomic write
# ===================================================================

class TestAtomicWrite:
    def test_writes_json_file(self, bus, tmp_path):
        """Atomic write creates a valid JSON file."""
        target = str(tmp_path / "test_state.json")
        data = {"source": "test", "ts": "2026-01-01T00:00:00Z"}
        bus._atomic_write_json(data, target)
        assert Path(target).exists()
        with open(target) as f:
            loaded = json.load(f)
        assert loaded["source"] == "test"


# ===================================================================
# 6. Overall state computation
# ===================================================================

class TestOverallState:
    def test_healthy(self, bus):
        """Most entities fresh = healthy."""
        entities = {
            "a": {"freshness_seconds": 10, "confidence": 0.99},
            "b": {"freshness_seconds": 20, "confidence": 0.98},
            "c": {"freshness_seconds": 30, "confidence": 0.97},
        }
        assert bus._compute_overall_state(entities, 10) == "healthy"

    def test_degraded(self, bus):
        """About half stale = degraded."""
        entities = {
            "a": {"freshness_seconds": 10, "confidence": 0.99},
            "b": {"freshness_seconds": 50000, "confidence": 0.2},
            "c": {"freshness_seconds": 60000, "confidence": 0.1},
            "d": {"freshness_seconds": 70000, "confidence": 0.05},
        }
        assert bus._compute_overall_state(entities, 10) in ("degraded", "stale")

    def test_empty_entities(self, bus):
        """No entities = unknown."""
        assert bus._compute_overall_state({}, None) == "unknown"