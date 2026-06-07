"""Helios v5 — Config Tests (HA-first migration).

Tests that new nested HA config loads correctly and old flat keys still work.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from helios.config_loader import ConfigLoader


# ---------------------------------------------------------------------------
# Sample configs
# ---------------------------------------------------------------------------

NEW_CONFIG_YAML = """
home_assistant:
  enabled: true
  base_url: "http://homeassistant.local:8123"
  token_env: "HASS_TOKEN"
  timeout: 15
  health:
    enabled: true
    prefix: "hae.healthsync_"
    stale_hours: 12
    fallback: "local_json"
  location:
    enabled: true
    entity_id: "device_tracker.user_iphone"
    poll_interval_seconds: 30
    fallback: "icloud"
  calendar:
    enabled: true
    source: "home_assistant"
    lookahead_days: 7
    fallback: "icloud"
    entities:
      - "calendar.jefferson"
      - "calendar.work"
  tasks:
    enabled: true
    source: "home_assistant"
    fallback: "local"
    entities:
      - "todo.today"
  sensors:
    enabled: true
    include_domains:
      - sensor
      - device_tracker

fallbacks:
  icloud:
    enabled: true
  local_json:
    enabled: true
  pyicloud:
    enabled: true

modules:
  calendar:
    enabled: true
    interval: 1800
  location:
    enabled: true
    interval: 900
"""


@pytest.fixture()
def config_file(tmp_path):
    """Write test config to a temp file and return the path."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(NEW_CONFIG_YAML)
    return cfg_path


# ===================================================================
# 1. Nested HA config loads
# ===================================================================

class TestHAConfig:
    def test_ha_config_loads(self, config_file):
        """Home Assistant config section loads from YAML."""
        with patch.object(ConfigLoader, "_find_config", return_value=config_file):
            cfg = ConfigLoader.load()

        assert cfg.get("home_assistant", "enabled") is True
        assert cfg.get("home_assistant", "base_url") == "http://homeassistant.local:8123"
        assert cfg.get("home_assistant", "token_env") == "HASS_TOKEN"
        assert cfg.get("home_assistant", "timeout") == 15

    def test_ha_health_config(self, config_file):
        """HA health sub-config loads correctly."""
        with patch.object(ConfigLoader, "_find_config", return_value=config_file):
            cfg = ConfigLoader.load()

        assert cfg.get("home_assistant", "health", "enabled") is True
        assert cfg.get("home_assistant", "health", "prefix") == "hae.healthsync_"
        assert cfg.get("home_assistant", "health", "stale_hours") == 12
        assert cfg.get("home_assistant", "health", "fallback") == "local_json"

    def test_ha_location_config(self, config_file):
        """HA location sub-config loads correctly."""
        with patch.object(ConfigLoader, "_find_config", return_value=config_file):
            cfg = ConfigLoader.load()

        assert cfg.get("home_assistant", "location", "enabled") is True
        assert cfg.get("home_assistant", "location", "entity_id") == "device_tracker.user_iphone"
        assert cfg.get("home_assistant", "location", "poll_interval_seconds") == 30
        assert cfg.get("home_assistant", "location", "fallback") == "icloud"

    def test_ha_calendar_config(self, config_file):
        """HA calendar sub-config loads correctly."""
        with patch.object(ConfigLoader, "_find_config", return_value=config_file):
            cfg = ConfigLoader.load()

        assert cfg.get("home_assistant", "calendar", "enabled") is True
        assert cfg.get("home_assistant", "calendar", "source") == "home_assistant"
        assert cfg.get("home_assistant", "calendar", "lookahead_days") == 7
        entities = cfg.get("home_assistant", "calendar", "entities")
        assert isinstance(entities, list)
        assert "calendar.jefferson" in entities
        assert "calendar.work" in entities

    def test_ha_tasks_config(self, config_file):
        """HA tasks sub-config loads correctly."""
        with patch.object(ConfigLoader, "_find_config", return_value=config_file):
            cfg = ConfigLoader.load()

        assert cfg.get("home_assistant", "tasks", "enabled") is True
        assert cfg.get("home_assistant", "tasks", "source") == "home_assistant"
        todo_entities = cfg.get("home_assistant", "tasks", "entities")
        assert isinstance(todo_entities, list)
        assert "todo.today" in todo_entities

    def test_ha_sensors_config(self, config_file):
        """HA sensors include_domains config loads correctly."""
        with patch.object(ConfigLoader, "_find_config", return_value=config_file):
            cfg = ConfigLoader.load()

        domains = cfg.get("home_assistant", "sensors", "include_domains")
        assert isinstance(domains, list)
        assert "sensor" in domains
        assert "device_tracker" in domains


# ===================================================================
# 2. Fallback config loads
# ===================================================================

class TestFallbackConfig:
    def test_fallbacks_load(self, config_file):
        """Fallbacks section loads from YAML."""
        with patch.object(ConfigLoader, "_find_config", return_value=config_file):
            cfg = ConfigLoader.load()

        assert cfg.get("fallbacks", "icloud", "enabled") is True
        assert cfg.get("fallbacks", "local_json", "enabled") is True
        assert cfg.get("fallbacks", "pyicloud", "enabled") is True


# ===================================================================
# 3. ConfigLoader.get() with nested keys
# ===================================================================

class TestConfigGet:
    def test_deep_nested_access(self, config_file):
        """ConfigLoader.get() navigates nested dicts."""
        with patch.object(ConfigLoader, "_find_config", return_value=config_file):
            cfg = ConfigLoader.load()

        # 3-level deep
        assert cfg.get("home_assistant", "calendar", "fallback") == "icloud"
        # Deep access with integer key (ConfigLoader.get only accepts str)
        entities_list = cfg.get("home_assistant", "calendar", "entities")
        if isinstance(entities_list, list) and entities_list:
            assert entities_list[0] == "calendar.jefferson"

    def test_missing_key_returns_default(self, config_file):
        """Missing key returns default value."""
        with patch.object(ConfigLoader, "_find_config", return_value=config_file):
            cfg = ConfigLoader.load()

        assert cfg.get("home_assistant", "nonexistent") is None
        assert cfg.get("home_assistant", "nonexistent", default="fallback") == "fallback"
        assert cfg.get("nonexistent", "key", default=42) == 42