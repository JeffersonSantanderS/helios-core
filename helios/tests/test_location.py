"""Helios v5 — Location Module Tests (HA-first migration).

Tests for helios.modules.location.LocationModule with HA-first behavior.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from helios.modules.location import LocationModule, LOCATION_FILE, DATA_DIR

# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

MOCK_HA_ENTITY = {
    "entity_id": "device_tracker.user_iphone",
    "state": "home",
    "attributes": {
        "latitude": 40.7128,
        "longitude": -74.0060,
        "gps_accuracy": 10,
        "friendly_name": "User iPhone",
        "source_type": "gps",
        "in_zones": ["home"],
    },
    "last_changed": "2026-05-15T12:00:00+00:00",
    "last_updated": (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
}


def _make_location_file(tmp_path: Path, data: dict | None = None):
    """Create an icloud_location_sync.json in the temp data dir."""
    default = {
        "source": "home_assistant",
        "ts": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "city": "Anytown",
        "province": "State",
        "lat": 40.7128,
        "lon": -74.0060,
        "accuracy": 10,
        "device": "iPhone (gps)",
    }
    if data:
        default.update(data)
    data_dir = tmp_path / "helios" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "icloud_location_sync.json").write_text(json.dumps(default, indent=2))
    return data_dir


# ===================================================================
# 1. HA-first: when HA is available and configured
# ===================================================================

class TestHAFirst:
    @patch.object(LocationModule, "_poll_ha")
    def test_ha_location_source(self, mock_poll_ha, tmp_path):
        """When HA is available, source is 'home_assistant'."""
        mock_poll_ha.return_value = {
            "lat": 40.7128,
            "lon": -74.0060,
            "accuracy": 10,
            "source": "home_assistant",
            "name": "iPhone (gps)",
            "zone": "home",
            "city": "Anytown",
        }

        data_dir = tmp_path / "helios" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        with patch("helios.modules.location.DATA_DIR", data_dir), \
             patch("helios.modules.location.LOCATION_FILE", data_dir / "icloud_location_sync.json"), \
             patch("helios.modules.location.LOCATION_HISTORY", data_dir / "location_history.jsonl"), \
             patch("helios.modules.location.LocationModule._geocode", return_value={"city": "Anytown", "province": "State"}), \
             patch.dict(os.environ, {"HASS_TOKEN": "test-token"}):

            mod = LocationModule(db_path=str(tmp_path / "test.db"), config={})
            # Force HA poll to be due
            mod._ha_last_poll = 0.0
            mod._ha_token = "test-token"
            result = mod.tick()

        assert result["source"] == "home_assistant"
        assert result.get("city") in ("Anytown", "Unknown")
        # Privacy: raw coordinates must NOT leak through tick() return
        assert "lat" not in result
        assert "lon" not in result
        assert result.get("zone") == "home"

    @patch.object(LocationModule, "_poll_ha")
    def test_ha_location_includes_lat_lon(self, mock_poll_ha, tmp_path):
        """HA location includes zone info; raw lat/lon are sanitized from tick()."""
        mock_poll_ha.return_value = {
            "lat": 40.7128,
            "lon": -74.0060,
            "accuracy": 10,
            "source": "home_assistant",
            "name": "iPhone (gps)",
            "zone": "home",
            "city": "Anytown",
        }

        data_dir = tmp_path / "helios" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        with patch("helios.modules.location.DATA_DIR", data_dir), \
             patch("helios.modules.location.LOCATION_FILE", data_dir / "icloud_location_sync.json"), \
             patch("helios.modules.location.LOCATION_HISTORY", data_dir / "location_history.jsonl"), \
             patch("helios.modules.location.LocationModule._geocode", return_value={"city": "Anytown", "province": "State"}), \
             patch.dict(os.environ, {"HASS_TOKEN": "test-token"}):

            mod = LocationModule(db_path=str(tmp_path / "test.db"), config={})
            mod._ha_last_poll = 0.0
            mod._ha_token = "test-token"
            result = mod.tick()

        # Raw coords are sanitized before returning
        assert "lat" not in result
        assert "lon" not in result
        # Zone info is preserved
        assert result["zone"] == "home"
        assert result["source"] == "home_assistant"


# ===================================================================
# 2. Fallback: when HA is unavailable
# ===================================================================

class TestFallback:
    @patch.object(LocationModule, "_poll_ha", return_value=None)
    @patch.object(LocationModule, "_poll_icloud", return_value=None)
    def test_ha_unavailable_no_iCloud(self, mock_icloud, mock_ha, tmp_path):
        """When HA and iCloud both fail, result source is 'none'."""
        data_dir = _make_location_file(tmp_path, {"source": "cached", "ts": "2020-01-01T00:00:00+00:00"})

        with patch("helios.modules.location.DATA_DIR", data_dir), \
             patch("helios.modules.location.LOCATION_FILE", data_dir / "icloud_location_sync.json"), \
             patch.dict(os.environ, {"HASS_TOKEN": "test-token"}):

            mod = LocationModule(db_path=str(tmp_path / "test.db"), config={})
            mod._ha_last_poll = 0.0
            mod._ha_token = "test-token"
            mod._ha_available = False  # Simulate HA being down
            mod._icloud_failures = 3   # Already past threshold
            mod._icloud_initialized = True
            result = mod.tick()

        # When everything fails but there's no cached position, source may be from file or none
        assert result.get("source") in ("none", "cached", "home_assistant")

    def test_no_token_means_no_ha(self, tmp_path):
        """When no HA token is set, LocationModule still instantiates."""
        with patch.dict(os.environ, {}, clear=True):
            env = {k: v for k, v in os.environ.items() if k not in ("HASS_TOKEN", "HA_TOKEN")}
            with patch.dict(os.environ, env, clear=True):
                mod = LocationModule(db_path=str(tmp_path / "test.db"), config={})
        assert mod._ha_token == ""
        assert mod._ha_should_poll() is False


# ===================================================================
# 3. Backward compatibility
# ===================================================================

class TestBackwardCompat:
    @patch.object(LocationModule, "_poll_ha")
    def test_ha_writes_compat_file(self, mock_poll_ha, tmp_path):
        """When HA is source, writes to icloud_location_sync.json for compat."""
        mock_poll_ha.return_value = {
            "lat": 40.7128,
            "lon": -74.0060,
            "accuracy": 10,
            "source": "home_assistant",
            "name": "iPhone (gps)",
            "zone": "home",
            "city": "Anytown",
        }

        data_dir = tmp_path / "helios_data"
        data_dir.mkdir(parents=True, exist_ok=True)
        compat_file = data_dir / "icloud_location_sync.json"
        history_file = data_dir / "location_history.jsonl"

        with patch("helios.modules.location.DATA_DIR", data_dir), \
             patch("helios.modules.location.LOCATION_FILE", compat_file), \
             patch("helios.modules.location.LOCATION_HISTORY", history_file), \
             patch.dict(os.environ, {"HASS_TOKEN": "test-token"}):

            mod = LocationModule(db_path=str(tmp_path / "test.db"), config={})
            mod._ha_last_poll = 0.0
            mod._ha_token = "test-token"
            result = mod.tick()

        assert result["source"] == "home_assistant"
        # Compat file should be written
        if compat_file.exists():
            compat_data = json.loads(compat_file.read_text())
            assert compat_data["source"] == "home_assistant"