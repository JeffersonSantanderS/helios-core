"""Tests for Home environment rules evaluation with real DB rules."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from typing import Any

import pytest

from helios.modules.home import HomeModule
from helios.rules_v2 import RulesEngine
from helios.state import HeliosDB


@pytest.fixture
def mock_home_context() -> dict[str, Any]:
    """Synthetic home context values that trigger specific rules."""
    return {
        "master_bedroom_occupied": True,
        "master_bedroom_occupied_count": 1,
        "master_bedroom_lux": 6.0,
        "master_bedroom_lux_max": 6.0,
        "master_bedroom_temp_c": 19.0,
        "master_bedroom_light_count": 0,
        "master_bedroom_light_brightness_avg": None,
        "master_bedroom_light_brightness_max": None,
        "spare_bedroom_occupied": False,
        "spare_bedroom_occupied_count": 0,
        "spare_bedroom_lux": 1.0,
        "spare_bedroom_lux_max": 1.0,
        "spare_bedroom_temp_c": 21.0,
        "spare_bedroom_light_count": 2,
        "spare_bedroom_light_brightness_avg": 69.4,
        "spare_bedroom_light_brightness_max": 69.4,
        "living_room_occupied": False,
        "living_room_occupied_count": 0,
        "living_room_lux": None,
        "living_room_lux_max": None,
        "living_room_temp_c": None,
        "living_room_light_count": 0,
        "living_room_light_brightness_avg": None,
        "living_room_light_brightness_max": None,
        "rooms_total": 3,
        "rooms_occupied": 1,
        "total_lights_on": 2,
        "source": "home_assistant",
    }


class TestRuleConditions:
    """Evaluate each home rule condition against known-good context states."""

    def _eval(self, expr: str, context: dict[str, Any]) -> bool:
        """Use the same logic as RulesEngine._eval_expr but without DB."""
        # Import just the evaluator method
        engine = RulesEngine(db=None)  # type: ignore[arg-type]
        return engine._eval_expr(expr, {"home": context})

    def test_room_too_hot_sleep_fires(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["master_bedroom_temp_c"] = 26.0
        assert self._eval("home.master_bedroom_temp_c > 24", ctx)

    def test_room_too_hot_sleep_does_not_fire(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["master_bedroom_temp_c"] = 19.0
        assert not self._eval("home.master_bedroom_temp_c > 24", ctx)

    def test_room_too_cold_sleep_fires(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["master_bedroom_temp_c"] = 14.0
        assert self._eval("home.master_bedroom_temp_c < 16", ctx)

    def test_room_too_cold_sleep_does_not_fire(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["master_bedroom_temp_c"] = 19.0
        assert not self._eval("home.master_bedroom_temp_c < 16", ctx)

    def test_energy_waste_fires(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["rooms_occupied"] = 0
        ctx["total_lights_on"] = 4
        assert self._eval("home.rooms_occupied < 1 and home.total_lights_on > 2", ctx)

    def test_energy_waste_does_not_fire(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["rooms_occupied"] = 1
        assert not self._eval("home.rooms_occupied < 1 and home.total_lights_on > 2", ctx)

    def test_master_bedroom_too_bright_fires(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["master_bedroom_lux"] = 120.0
        assert self._eval("home.master_bedroom_lux > 100", ctx)

    def test_master_bedroom_too_bright_does_not_fire(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["master_bedroom_lux"] = 6.0
        assert not self._eval("home.master_bedroom_lux > 100", ctx)

    def test_motion_night_lights_off_fires(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["master_bedroom_occupied"] = True
        ctx["master_bedroom_light_count"] = 0
        assert self._eval("home.master_bedroom_occupied == True AND home.master_bedroom_light_count == 0", ctx)

    def test_motion_night_lights_off_does_not_fire(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["master_bedroom_occupied"] = True
        ctx["master_bedroom_light_count"] = 2
        assert not self._eval("home.master_bedroom_occupied == True AND home.master_bedroom_light_count == 0", ctx)

    def test_concurrent_occupied_both_fires(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["master_bedroom_occupied"] = True
        ctx["spare_bedroom_occupied"] = True
        assert self._eval("home.master_bedroom_occupied == True and home.spare_bedroom_occupied == True", ctx)

    def test_concurrent_occupied_both_does_not_fire(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["master_bedroom_occupied"] = True
        ctx["spare_bedroom_occupied"] = False
        assert not self._eval("home.master_bedroom_occupied == True and home.spare_bedroom_occupied == True", ctx)

    def test_spare_room_hot_fires(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["spare_bedroom_temp_c"] = 27.0
        assert self._eval("home.spare_bedroom_temp_c > 25", ctx)

    def test_spare_room_hot_does_not_fire(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["spare_bedroom_temp_c"] = 21.0
        assert not self._eval("home.spare_bedroom_temp_c > 25", ctx)

    def test_all_lights_off_fires(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["total_lights_on"] = 4
        assert self._eval("home.total_lights_on > 3", ctx)

    def test_all_lights_off_does_not_fire(self, mock_home_context):
        ctx = {**mock_home_context}
        ctx["total_lights_on"] = 3
        assert not self._eval("home.total_lights_on > 3", ctx)


class TestLiveRules:
    """Evaluate real DB rules against current HA state."""

    @pytest.mark.skipif(
        not os.path.exists(os.path.expanduser("~/.hermes/.env")),
        reason="Requires local ~/.hermes/.env with HASS_TOKEN"
    )
    def test_all_rules_evaluate_without_error(self):
        """Every enabled home rule must evaluate without crashing."""
        db = HeliosDB()
        engine = RulesEngine(db)

        # Build live context
        env = {}
        with open(os.path.expanduser("~/.hermes/.env"), 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k] = v

        config = {
            "enabled": True, "ha_enabled": True,
            "ha_base_url": "http://homeassistant.local:8123",
            "ha_token": env.get("HASS_TOKEN", ""),
            "rooms": {
                "master_bedroom": {
                    "motion": ["binary_sensor.master_bedroom_motion_2"],
                    "illuminance": ["sensor.master_bedroom_illuminance_2"],
                    "temperature": ["sensor.master_bedroom_temperature_2"],
                    "lights": ["light.master_bedroom", "light.window_light", "light.window_light_2", "light.closet_light"],
                },
                "spare_bedroom": {
                    "motion": ["binary_sensor.spare_bedroom_motion_2"],
                    "illuminance": ["sensor.spare_bedroom_illuminance_2"],
                    "temperature": ["sensor.spare_bedroom_temperature"],
                    "lights": ["light.desk_light", "light.wardrobe_light"],
                },
                "living_room": {
                    "motion": [], "illuminance": [], "temperature": [],
                    "lights": ["light.lamp_light_1", "light.lamp_light_2", "light.lamp_light_3"],
                },
            },
        }
        mod = HomeModule(db_path=db.db_path, config=config)
        home_ctx = mod.tick()
        engine_ctx = {"home": home_ctx}

        conn = sqlite3.connect(db.db_path)
        conn.row_factory = sqlite3.Row
        rules = conn.execute(
            "SELECT slug, condition FROM rules WHERE enabled = 1 AND category = 'environment'"
        ).fetchall()
        conn.close()

        for slug, cond in rules:
            # Must evaluate without exception
            try:
                result = engine._eval_expr(cond, engine_ctx)
                assert isinstance(result, bool)  # noqa: S101
            except Exception as exc:
                pytest.fail(f"Rule '{slug}' crashed: {exc}")
