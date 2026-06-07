"""Helios v6 — Home environment module.

Reads Home Assistant environmental sensors (motion, light, illuminance, temperature)
and computes per-room context keys. HA entity IDs are mapped to physical rooms via
the configurable `rooms` dict so renames in HA don't require code changes.

Context keys written (per room, e.g. home.master_bedroom.*):
    occupied                (bool)  Any motion sensor ON in the room
    occupied_count          (int)   Number of motion sensors currently ON
    lux                     (float) Average illuminance across valid sensors
    lux_max                 (float) Max illuminance in the room
    temp_c                  (float) Temperature (first valid sensor)
    light_count             (int)   Number of lights ON in the room
    light_brightness_avg    (float) Average brightness % across ON lights
    light_brightness_max    (float) Max brightness % across ON lights
    source                  (str)   "home_assistant" or "none"

Aggregate keys (home.*):
    rooms_total             (int)   Total configured rooms
    rooms_occupied          (int)   Rooms with any motion
    total_lights_on         (int)   Lights ON across all rooms

Also writes a flat context_export.json compatible with the stable exporter.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

try:
    from ..ha_client import fetch_all_states as _ha_fetch_states
    HA_CLIENT_AVAILABLE = True
except ImportError:
    HA_CLIENT_AVAILABLE = False

    def _ha_fetch_states(*args, **kwargs):  # type: ignore[misc]
        return []

from .base import BaseMod

logger = logging.getLogger("helios.home")

MODULE_NAME = "home"
SOURCE = "script_engine"
_CONTEXT_UPSERT_SQL = """
INSERT INTO context (source, module, key, value, priority)
VALUES (:source, :module, :key, :value, :priority)
ON CONFLICT (module, key, source) DO UPDATE SET
    value   = excluded.value,
    ts      = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    priority = excluded.priority
"""

# Map HA entity states to physical rooms; keys = room_id, values = sensor lists.
# Only motion, illuminance, and temperature are auto-filtered for unavailable.
DEFAULT_ROOM_ENTITIES: dict[str, dict[str, list[str]]] = {
    "master_bedroom": {
        "motion": ["binary_sensor.master_bedroom_motion_2"],
        "illuminance": ["sensor.master_bedroom_illuminance_2"],
        "temperature": ["sensor.master_bedroom_temperature_2"],
        "lights": [
            "light.master_bedroom",
            "light.window_light",
            "light.window_light_2",
            "light.closet_light",
        ],
    },
    "spare_bedroom": {
        "motion": ["binary_sensor.spare_bedroom_motion_2"],
        "illuminance": ["sensor.spare_bedroom_illuminance_2"],
        "temperature": ["sensor.spare_bedroom_temperature"],
        "lights": [
            "light.desk_light",
            "light.wardrobe_light",
        ],
    },
    "living_room": {
        "motion": [],
        "illuminance": [],
        "temperature": [],
        "lights": [
            "light.lamp_light_1",
            "light.lamp_light_2",
            "light.lamp_light_3",
        ],
    },
}

# Device trackers for presence detection. State "home" means the person is home.
# Configurable via modules.home.device_trackers in config.yaml.
DEFAULT_DEVICE_TRACKERS: list[str] = [
    os.environ.get("ICLOUD_DEVICE_TRACKER", ""),
]


def _parse_numeric(value: Any) -> float | None:
    if value in (None, "unavailable", "unknown", "", "None"):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


class HomeModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "home",
        "version": "1.0.0",
        "description": "Environmental sensor ingestion from Home Assistant with room mapping",
        "author": "system",
        "collectors": ["home_assistant"],
        "dependencies": [],
        "priority": 8,
    }

    def __init__(self, db_path: str | None = None, config: dict | None = None) -> None:
        super().__init__(db_path=db_path, config=config)
        self.config = config or {}
        self._ha_enabled: bool = self.config.get("ha_enabled", True)
        self._ha_base_url: str = self.config.get(
            "ha_base_url",
            os.environ.get("HASS_URL", "")
            or os.environ.get("HOME_ASSISTANT_URL", "")
            or "",
        )
        self._ha_token: str = self.config.get(
            "ha_token", ""
        ) or os.environ.get("HASS_TOKEN", "") or os.environ.get("HA_TOKEN", "")
        self._rooms: dict[str, dict[str, list[str]]] = self.config.get(
            "rooms", DEFAULT_ROOM_ENTITIES
        )
        self._device_trackers: list[str] = self.config.get(
            "device_trackers", DEFAULT_DEVICE_TRACKERS
        )

    # ------------------------------------------------------------------
    # HA fetch (primary)
    # ------------------------------------------------------------------

    def _fetch_ha_states(self) -> dict[str, dict[str, Any]]:
        """Fetch all HA states and return a lookup dict keyed by entity_id."""
        if not HA_CLIENT_AVAILABLE:
            logger.debug("HA client not available")
            return {}
        if not self._ha_enabled:
            logger.debug("HA home disabled")
            return {}
        if not self._ha_base_url or not self._ha_token:
            logger.debug("HA home not configured")
            return {}

        states = _ha_fetch_states(
            base_url=self._ha_base_url,
            token=self._ha_token,
            timeout=15,
        )
        if states:
            logger.debug("Fetched %d HA states for home module", len(states))
        return {s["entity_id"]: s for s in states}

    # ------------------------------------------------------------------
    # Room aggregation
    # ------------------------------------------------------------------

    @staticmethod
    def _brightness_pct(entity: dict[str, Any]) -> float | None:
        """Convert HA brightness (0-255) to percentage. Returns None if light is off or missing attr."""
        if entity.get("state") != "on":
            return None
        brightness = entity.get("attributes", {}).get("brightness")
        if brightness is None:
            return None
        try:
            return round(float(brightness) / 255.0 * 100.0, 1)
        except (ValueError, TypeError):
            return None

    def _compute_room(self, room_id: str, states: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Compute context keys for a single room from HA states.

        Filters:
          - motion/illuminance/temperature: skip unavailable/unknown/None
          - lights: any state is valid (including 'off')
        """
        cfg = self._rooms.get(room_id, {})
        result: dict[str, Any] = {
            "occupied": False,
            "occupied_count": 0,
            "lux": None,
            "lux_max": None,
            "temp_c": None,
            "light_count": 0,
            "light_brightness_avg": None,
            "light_brightness_max": None,
        }

        # Motion
        valid_motion_states = ["on", "off"]  # treat any non-unavailable as valid
        motion_on = 0
        for eid in cfg.get("motion", []):
            entity = states.get(eid)
            if not entity:
                continue
            state = str(entity.get("state", "")).lower()
            if state in ("unavailable", "unknown", "none"):
                continue
            if state == "on":
                motion_on += 1
                result["occupied"] = True
        result["occupied_count"] = motion_on

        # Illuminance
        lux_values: list[float] = []
        for eid in cfg.get("illuminance", []):
            entity = states.get(eid)
            if not entity:
                continue
            parsed = _parse_numeric(entity.get("state"))
            if parsed is not None:
                lux_values.append(parsed)
        if lux_values:
            result["lux"] = round(sum(lux_values) / len(lux_values), 1)
            result["lux_max"] = max(lux_values)

        # Temperature
        for eid in cfg.get("temperature", []):
            entity = states.get(eid)
            if not entity:
                continue
            parsed = _parse_numeric(entity.get("state"))
            if parsed is not None:
                result["temp_c"] = round(parsed, 1)
                break  # first valid sensor wins

        # Lights
        brightness_values: list[float] = []
        lights_on = 0
        for eid in cfg.get("lights", []):
            entity = states.get(eid)
            if not entity:
                continue
            state = entity.get("state", "")
            if state == "on":
                lights_on += 1
            pct = self._brightness_pct(entity)
            if pct is not None:
                brightness_values.append(pct)
        result["light_count"] = lights_on
        if brightness_values:
            result["light_brightness_avg"] = round(sum(brightness_values) / len(brightness_values), 1)
            result["light_brightness_max"] = max(brightness_values)

        return result

    def _compute_aggregate(self, room_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Compute house-level aggregates across all rooms."""
        total_lights = sum(r["light_count"] for r in room_results.values())
        rooms_occ = sum(1 for r in room_results.values() if r["occupied"])
        return {
            "rooms_total": len(room_results),
            "rooms_occupied": rooms_occ,
            "total_lights_on": total_lights,
        }

    def _compute_presence(self, states: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Check device trackers for presence. Returns anyone_home and per-device status."""
        anyone_home = False
        devices_home: dict[str, bool] = {}
        for tracker_id in self._device_trackers:
            entity = states.get(tracker_id, {})
            state = entity.get("state", "unknown")
            is_home = state == "home"
            devices_home[tracker_id] = is_home
            if is_home:
                anyone_home = True
        return {
            "anyone_home": anyone_home,
            "devices_home": devices_home,
        }

    # ------------------------------------------------------------------
    # Context persistence
    # ------------------------------------------------------------------

    def _write_context(
        self, conn: sqlite3.Connection, context_data: dict[str, Any]
    ) -> int:
        written = 0
        for key_name, value in context_data.items():
            json_value = json.dumps(value, default=str)
            conn.execute(
                _CONTEXT_UPSERT_SQL,
                {
                    "source": SOURCE,
                    "module": MODULE_NAME,
                    "key": key_name,
                    "value": json_value,
                    "priority": 0,
                },
            )
            written += 1
        return written

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        """Main scheduler entry point.

        Returns a flat dict with keys that the rules engine can evaluate
        (module.key → at most one dot).
        """
        # Flat keys for rules evaluation
        rule_context: dict[str, Any] = {
            "source": "none",
            "rooms_total": len(self._rooms),
        }

        # Fetch HA states
        states = self._fetch_ha_states()
        if states:
            rule_context["source"] = "home_assistant"

        # Compute per-room
        room_results: dict[str, dict[str, Any]] = {}
        for room_id in self._rooms:
            room_data = self._compute_room(room_id, states)
            room_results[room_id] = room_data
            # Flatten for rules engine   (module=home, key=master_bedroom_occupied)
            for key, val in room_data.items():
                rule_context[f"{room_id}_{key}"] = val

        # House aggregates (already flat)
        agg = self._compute_aggregate(room_results)
        for key, val in agg.items():
            rule_context[key] = val

        # Presence from device trackers
        if states:
            presence = self._compute_presence(states)
            rule_context["anyone_home"] = presence["anyone_home"]
            rule_context["devices_home"] = presence["devices_home"]

        # Build dotted keys for DB context (human-readable exports)
        db_context: dict[str, Any] = {"source": rule_context["source"]}
        for room_id in self._rooms:
            room_data = room_results.get(room_id, {})
            for key, val in room_data.items():
                db_context[f"home.{room_id}.{key}"] = val
        for key, val in agg.items():
            db_context[f"home.{key}"] = val
        # Presence
        if "anyone_home" in rule_context:
            db_context["home.anyone_home"] = rule_context["anyone_home"]
            for tracker_id, is_home in rule_context.get("devices_home", {}).items():
                # e.g. home.device_tracker.<device_name> -> True
                short_id = tracker_id.replace("device_tracker.", "")
                db_context[f"home.device_{short_id}_home"] = is_home

        # Write to DB using descriptive dotted keys
        if self.db_path and db_context:
            try:
                conn = sqlite3.connect(self.db_path)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                written = self._write_context(conn, db_context)
                conn.commit()
                conn.close()
                rule_context["context_written"] = written
            except Exception as exc:
                logger.warning("Failed to write home context: %s", exc)

        return rule_context
