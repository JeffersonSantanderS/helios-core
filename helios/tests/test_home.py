"""Tests for the Home (environment) module."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from typing import Any

import pytest

from helios.modules.home import HomeModule, DEFAULT_ROOM_ENTITIES


@pytest.fixture
def fresh_db():
    """Provide a temporary SQLite DB with the context table ready."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS context (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            source      TEXT    NOT NULL,
            module      TEXT    NOT NULL,
            key         TEXT    NOT NULL,
            value       TEXT    NOT NULL DEFAULT '{}',
            priority    INTEGER NOT NULL DEFAULT 0,
            expires_at  TEXT,
            created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            CONSTRAINT ctx_unique_latest UNIQUE (module, key, source)
        )
    """)
    conn.commit()
    conn.close()
    yield path
    os.remove(path)


@pytest.fixture
def config_with_ha():
    """Module config wired to the local HA test env."""
    return {
        "enabled": True,
        "ha_enabled": True,
        "ha_base_url": "http://homeassistant.local:8123",
        "rooms": DEFAULT_ROOM_ENTITIES,
    }


class TestBrightnessHelper:
    """Unit tests for the static brightness converter."""

    def test_brightness_pct_on(self):
        mod = HomeModule(db_path=":memory:", config={})
        entity = {"state": "on", "attributes": {"brightness": 255}}
        assert mod._brightness_pct(entity) == 100.0

    def test_brightness_pct_half(self):
        mod = HomeModule(db_path=":memory:", config={})
        entity = {"state": "on", "attributes": {"brightness": 128}}
        pct = mod._brightness_pct(entity)
        # 128/255*100 = 50.196...
        assert 50.0 <= pct <= 50.2

    def test_brightness_pct_off_returns_none(self):
        mod = HomeModule(db_path=":memory:", config={})
        entity = {"state": "off", "attributes": {"brightness": 128}}
        assert mod._brightness_pct(entity) is None

    def test_brightness_pct_no_attr(self):
        mod = HomeModule(db_path=":memory:", config={})
        entity = {"state": "on", "attributes": {}}
        assert mod._brightness_pct(entity) is None


class TestRoomComputation:
    """Unit tests for _compute_room with synthetic HA states."""

    def test_empty_room_no_sensors(self):
        """Room with no sensors configured should return default zeros."""
        mod = HomeModule(db_path=":memory:", config={
            "rooms": {"empty_room": {"motion": [], "illuminance": [], "temperature": [], "lights": []}}
        })
        states = {}
        result = mod._compute_room("empty_room", states)
        assert result["occupied"] is False
        assert result["occupied_count"] == 0
        assert result["light_count"] == 0
        assert result["lux"] is None
        assert result["temp_c"] is None

    def test_motion_on(self):
        mod = HomeModule(db_path=":memory:", config={
            "rooms": {"test": {"motion": ["binary_sensor.test_motion"], "lights": []}}
        })
        states = {"binary_sensor.test_motion": {"state": "on", "attributes": {}}}
        result = mod._compute_room("test", states)
        assert result["occupied"] is True
        assert result["occupied_count"] == 1

    def test_motion_off(self):
        mod = HomeModule(db_path=":memory:", config={
            "rooms": {"test": {"motion": ["binary_sensor.test_motion"], "lights": []}}
        })
        states = {"binary_sensor.test_motion": {"state": "off", "attributes": {}}}
        result = mod._compute_room("test", states)
        assert result["occupied"] is False
        assert result["occupied_count"] == 0

    def test_motion_unavailable_skipped(self):
        mod = HomeModule(db_path=":memory:", config={
            "rooms": {"test": {
                "motion": ["binary_sensor.dead", "binary_sensor.live"],
                "lights": []
            }}
        })
        states = {
            "binary_sensor.dead": {"state": "unavailable", "attributes": {}},
            "binary_sensor.live": {"state": "on", "attributes": {}},
        }
        result = mod._compute_room("test", states)
        assert result["occupied"] is True
        assert result["occupied_count"] == 1

    def test_illuminance_avg_and_max(self):
        mod = HomeModule(db_path=":memory:", config={
            "rooms": {"test": {
                "illuminance": ["sensor.1", "sensor.2"],
                "lights": []
            }}
        })
        states = {
            "sensor.1": {"state": "10", "attributes": {"unit_of_measurement": "lx"}},
            "sensor.2": {"state": "20", "attributes": {"unit_of_measurement": "lx"}},
        }
        result = mod._compute_room("test", states)
        assert result["lux"] == 15.0
        assert result["lux_max"] == 20.0

    def test_temperature_first_valid(self):
        mod = HomeModule(db_path=":memory:", config={
            "rooms": {"test": {
                "temperature": ["sensor.dead", "sensor.live"],
                "lights": []
            }}
        })
        states = {
            "sensor.dead": {"state": "unavailable", "attributes": {}},
            "sensor.live": {"state": "22.5", "attributes": {"unit_of_measurement": "°C"}},
        }
        result = mod._compute_room("test", states)
        assert result["temp_c"] == 22.5

    def test_lights_count_and_brightness(self):
        mod = HomeModule(db_path=":memory:", config={
            "rooms": {"test": {
                "lights": ["light.1", "light.2", "light.3"]
            }}
        })
        states = {
            "light.1": {"state": "on", "attributes": {"brightness": 255}},
            "light.2": {"state": "on", "attributes": {"brightness": 128}},
            "light.3": {"state": "off", "attributes": {}},
        }
        result = mod._compute_room("test", states)
        assert result["light_count"] == 2
        # (100 + 50.196... ) / 2 = 75.098...
        assert 74.0 <= result["light_brightness_avg"] <= 76.0
        assert result["light_brightness_max"] == 100.0

    def test_lights_missing_entity(self):
        mod = HomeModule(db_path=":memory:", config={
            "rooms": {"test": {"lights": ["light.gone"]}}
        })
        states = {}
        result = mod._compute_room("test", states)
        assert result["light_count"] == 0
        assert result["light_brightness_avg"] is None


class TestAggregateComputation:
    """Unit tests for house-wide aggregation."""

    def test_aggregate_basics(self):
        mod = HomeModule(db_path=":memory:", config={})
        room_results = {
            "master_bedroom": {"occupied": True, "light_count": 2},
            "spare_bedroom": {"occupied": False, "light_count": 1},
            "living_room": {"occupied": False, "light_count": 0},
        }
        agg = mod._compute_aggregate(room_results)
        assert agg["rooms_total"] == 3
        assert agg["rooms_occupied"] == 1
        assert agg["total_lights_on"] == 3

    def test_aggregate_empty(self):
        mod = HomeModule(db_path=":memory:", config={})
        agg = mod._compute_aggregate({})
        assert agg["rooms_total"] == 0
        assert agg["rooms_occupied"] == 0
        assert agg["total_lights_on"] == 0


class TestTickWithMockedStates:
    """Tick-level tests using a monkeypatched _fetch_ha_states."""

    def test_tick_with_ha_states_writes_context(self, fresh_db, config_with_ha):
        mod = HomeModule(db_path=fresh_db, config=config_with_ha)

        # Monkeypatch HA fetch
        mod._fetch_ha_states = lambda: {
            "binary_sensor.master_bedroom_motion_2": {"state": "on", "attributes": {}},
            "sensor.master_bedroom_illuminance_2": {"state": "45", "attributes": {"unit_of_measurement": "lx"}},
            "sensor.master_bedroom_temperature_2": {"state": "19.5", "attributes": {"unit_of_measurement": "°C"}},
            "light.master_bedroom": {"state": "on", "attributes": {"brightness": 128}},
            "light.window_light": {"state": "off", "attributes": {}},
            "light.window_light_2": {"state": "off", "attributes": {}},
            "light.closet_light": {"state": "off", "attributes": {}},
            "binary_sensor.spare_bedroom_motion_2": {"state": "off", "attributes": {}},
            "sensor.spare_bedroom_illuminance_2": {"state": "3", "attributes": {"unit_of_measurement": "lx"}},
            "sensor.spare_bedroom_temperature": {"state": "21.0", "attributes": {"unit_of_measurement": "°C"}},
            "light.desk_light": {"state": "on", "attributes": {"brightness": 255}},
            "light.wardrobe_light": {"state": "off", "attributes": {}},
            "light.lamp_light_1": {"state": "off", "attributes": {}},
            "light.lamp_light_2": {"state": "off", "attributes": {}},
            "light.lamp_light_3": {"state": "off", "attributes": {}},
        }

        result = mod.tick()

        # Source
        assert result["source"] == "home_assistant"

        # Master bedroom (flat keys for rules engine)
        assert result["master_bedroom_occupied"] is True
        assert result["master_bedroom_occupied_count"] == 1
        assert result["master_bedroom_lux"] == 45.0
        assert result["master_bedroom_temp_c"] == 19.5
        assert result["master_bedroom_light_count"] == 1
        assert result["master_bedroom_light_brightness_max"] == 50.2  # 128/255

        # Spare bedroom
        assert result["spare_bedroom_occupied"] is False
        assert result["spare_bedroom_lux"] == 3.0
        assert result["spare_bedroom_temp_c"] == 21.0
        assert result["spare_bedroom_light_count"] == 1
        assert result["spare_bedroom_light_brightness_max"] == 100.0

        # Living room
        assert result["living_room_light_count"] == 0
        assert result["living_room_occupied"] is False

        # Aggregates
        assert result["rooms_total"] == 3
        assert result["rooms_occupied"] == 1
        assert result["total_lights_on"] == 2

        # DB persistence (dotted keys stored in DB)
        assert result.get("context_written", 0) > 0

        conn = sqlite3.connect(fresh_db)
        rows = conn.execute(
            "SELECT key, value FROM context WHERE module = 'home' AND key LIKE 'home.master_bedroom.%'"
        ).fetchall()
        keys = {r[0] for r in rows}
        assert "home.master_bedroom.occupied" in keys
        assert "home.master_bedroom.lux" in keys
        conn.close()

    def test_tick_writes_correct_per_room_db_keys(self, fresh_db, config_with_ha):
        """Each room's dotted DB keys must match that room's data, not the last room's."""
        mod = HomeModule(db_path=fresh_db, config=config_with_ha)

        mod._fetch_ha_states = lambda: {
            "binary_sensor.master_bedroom_motion_2": {"state": "on", "attributes": {}},
            "sensor.master_bedroom_illuminance_2": {"state": "100", "attributes": {"unit_of_measurement": "lx"}},
            "sensor.master_bedroom_temperature_2": {"state": "25.0", "attributes": {"unit_of_measurement": "°C"}},
            "light.master_bedroom": {"state": "on", "attributes": {"brightness": 255}},
            "light.window_light": {"state": "off", "attributes": {}},
            "light.window_light_2": {"state": "off", "attributes": {}},
            "light.closet_light": {"state": "off", "attributes": {}},
            "binary_sensor.spare_bedroom_motion_2": {"state": "off", "attributes": {}},
            "sensor.spare_bedroom_illuminance_2": {"state": "5", "attributes": {"unit_of_measurement": "lx"}},
            "sensor.spare_bedroom_temperature": {"state": "18.0", "attributes": {"unit_of_measurement": "°C"}},
            "light.desk_light": {"state": "off", "attributes": {}},
            "light.wardrobe_light": {"state": "off", "attributes": {}},
            "light.lamp_light_1": {"state": "off", "attributes": {}},
            "light.lamp_light_2": {"state": "off", "attributes": {}},
            "light.lamp_light_3": {"state": "off", "attributes": {}},
        }

        result = mod.tick()
        assert result["master_bedroom_occupied"] is True
        assert result["master_bedroom_temp_c"] == 25.0
        assert result["spare_bedroom_occupied"] is False
        assert result["spare_bedroom_temp_c"] == 18.0

        conn = sqlite3.connect(fresh_db)
        rows = conn.execute(
            "SELECT key, value FROM context WHERE module = 'home'"
        ).fetchall()
        db = {r[0]: json.loads(r[1]) for r in rows}
        conn.close()

        # Master bedroom DB values must match master bedroom tick values
        assert db["home.master_bedroom.occupied"] is True
        assert db["home.master_bedroom.lux"] == 100.0
        assert db["home.master_bedroom.temp_c"] == 25.0
        assert db["home.master_bedroom.light_count"] == 1

        # Spare bedroom DB values must match spare bedroom tick values
        assert db["home.spare_bedroom.occupied"] is False
        assert db["home.spare_bedroom.lux"] == 5.0
        assert db["home.spare_bedroom.temp_c"] == 18.0
        assert db["home.spare_bedroom.light_count"] == 0

        # Living room should still be present with zeros
        assert db["home.living_room.light_count"] == 0
        assert db["home.living_room.occupied"] is False

    def test_tick_without_ha_returns_none_source(self, fresh_db):
        mod = HomeModule(db_path=fresh_db, config={
            "enabled": True,
            "ha_enabled": False,
            "rooms": DEFAULT_ROOM_ENTITIES,
        })
        result = mod.tick()
        assert result["source"] == "none"
        assert result["rooms_total"] == 3
        # All rooms should have None / zero because no HA data
        assert result["master_bedroom_occupied"] is False
        assert result["master_bedroom_lux"] is None


class TestManifest:
    """Verify module discovery metadata."""

    def test_manifest_name_and_version(self):
        manifest = HomeModule.MODULE_MANIFEST
        assert manifest["name"] == "home"
        assert manifest["version"] == "1.0.0"
        assert "environmental sensor" in manifest["description"].lower()

    def test_module_info_returns_enabled(self):
        mod = HomeModule(db_path=":memory:", config={"enabled": True})
        info = mod.module_info()
        assert info["enabled"] is True
        assert info["name"] == "home"

    def test_module_info_disabled(self):
        mod = HomeModule(db_path=":memory:", config={"enabled": False})
        info = mod.module_info()
        assert info["enabled"] is False
