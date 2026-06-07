import os
"""Helios v6 — Location module (Home Assistant primary, iCloud fallback).

Refactored with:
  - ZoneResolver abstraction: raw GPS → zone → dwell → POI → sanitized event
  - POIProvider with confidence scoring and teachable memory
  - Privacy sanitizer: no raw lat/lon in logs, exports, or notifications
  - Config flags: poi_lookup_enabled, poi_provider, poi_min_dwell_seconds
"""

from .base import BaseMod
from typing import Any
import json, logging, os, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .zone_resolver import LocationZoneResolver
from .location_poi import POIProvider
from .privacy_sanitizer import sanitize_location_event, sanitize_log_message

log = logging.getLogger("helios.location")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
LOCATION_FILE = DATA_DIR / "icloud_location_sync.json"
LOCATION_HISTORY = DATA_DIR / "location_history.jsonl"
CREDS_FILE = DATA_DIR / "icloud_creds.json"
COOKIE_DIR = os.path.expanduser("~/.hermes/helios-v6/.icloud_session")

# HA config
HA_BASE_URL = os.environ.get("HOME_ASSISTANT_URL", "") or os.environ.get("HASS_URL", "") or ""
HA_ENTITY = os.environ.get("ICLOUD_DEVICE_TRACKER", "") or os.environ.get("HA_DEVICE_TRACKER", "")

# Geocode fallback defaults
DEFAULT_CITY = os.environ.get("HELIOS_DEFAULT_CITY", "")
DEFAULT_PROVINCE = os.environ.get("HELIOS_DEFAULT_PROVINCE", "")

# iCloud fallback
ICLOUD_POLL_INTERVAL_SEC = 900
MAX_ICLOUD_CALLS_PER_HOUR = 3
HA_POLL_INTERVAL_SEC = 30


class LocationModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "location",
        "version": "3.1.0",
        "description": "Tracks iPhone GPS via HA (iCloud fallback) with POI dwell detection",
        "author": "Helios",
        "collectors": ["icloud_location_sync.json"],
        "dependencies": ["requests"],
        "priority": 1,
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._ha_token: str = ""
        self._ha_available: bool = True
        self._ha_last_poll: float = 0.0

        # iCloud fallback state
        self._api = None
        self._icloud_last_poll: float = 0.0
        self._icloud_calls_this_hour: int = 0
        self._icloud_hour_start: float = 0.0
        self._icloud_initialized: bool = False
        self._icloud_failures: int = 0

        # Cached last known position
        self._last_lat: float | None = None
        self._last_lon: float | None = None
        self._last_zone: str | None = None

        # Load config
        self._poi_enabled = self.config.get("poi_lookup_enabled", False)
        self._poi_provider_name = self.config.get("poi_provider", "overpass")
        self._poi_min_dwell = self.config.get("poi_min_dwell_seconds", 600)

        poi_config = {
            "poi_lookup_enabled": self._poi_enabled,
            "poi_provider": self._poi_provider_name,
            "poi_min_dwell_seconds": self._poi_min_dwell,
        }
        self._poi_provider = POIProvider(config=poi_config)
        self._zone_resolver = LocationZoneResolver(
            poi_provider=self._poi_provider,
            config=poi_config,
        )

        self._load_token()

    def _load_token(self) -> None:
        self._ha_token = (
            os.environ.get("HASS_TOKEN", "")
            or os.environ.get("HA_TOKEN", "")
        )
        if self._ha_token:
            log.debug("HA token loaded (%d chars)", len(self._ha_token))
        else:
            log.warning("No HASS_TOKEN or HA_TOKEN — HA location unavailable")

    # ── HA API ─────────────────────────────────────────────────────────

    def _poll_ha(self) -> dict | None:
        if not self._ha_token:
            return None
        try:
            import urllib.request
            import urllib.error

            url = f"{HA_BASE_URL}/api/states/{HA_ENTITY}"
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {self._ha_token}")
            req.add_header("Content-Type", "application/json")

            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read())

            state = data.get("state", "")
            attrs = data.get("attributes", {})
            lat = attrs.get("latitude")
            lon = attrs.get("longitude")
            acc = attrs.get("gps_accuracy")
            src = attrs.get("source_type", "unknown")
            zones = attrs.get("in_zones", [])

            if lat is None or lon is None:
                log.debug("HA returned no coordinates (state=%s)", state)
                return None

            self._ha_last_poll = time.time()
            self._icloud_failures = 0

            return {
                "lat": lat,
                "lon": lon,
                "accuracy": acc,
                "source": "home_assistant",
                "name": f"iPhone ({src})",
                "zone": zones[0] if zones else "away",
            }

        except urllib.error.HTTPError as exc:
            log.warning("HA API HTTP %d: %s", exc.code, exc.reason)
            return None
        except urllib.error.URLError as exc:
            log.warning("HA API unreachable: %s", exc.reason)
            self._ha_available = False
            return None
        except Exception as exc:
            log.warning("HA poll error: %s", exc)
            return None

    def _ha_should_poll(self) -> bool:
        if not self._ha_token:
            return False
        return time.time() - self._ha_last_poll >= HA_POLL_INTERVAL_SEC

    # ── iCloud fallback ────────────────────────────────────────────────

    def _load_creds(self) -> dict:
        if not CREDS_FILE.exists():
            return {}
        try:
            return json.loads(CREDS_FILE.read_text())
        except Exception:
            return {}

    def _get_api(self) -> Any | None:
        if self._api is not None:
            return self._api

        creds = self._load_creds()
        apple_id = creds.get("apple_id", "")
        password = creds.get("password", "")

        if not apple_id:
            log.warning("No apple_id in icloud_creds.json")
            return None

        if password in ("", "***"):
            password = os.environ.get("AID", "")
            if not password:
                log.warning("iCloud password not available")
                return None

        try:
            from pyicloud import PyiCloudService

            if os.path.isdir(COOKIE_DIR):
                api = PyiCloudService(apple_id, password, cookie_directory=COOKIE_DIR)
            else:
                api = PyiCloudService(apple_id, password)

            if api.requires_2fa or api.requires_2sa:
                log.warning("iCloud session needs 2FA")
                return None

            log.debug("iCloud session ready (trusted=%s)", api.is_trusted_session)
            self._api = api
            self._icloud_last_poll = 0.0
            self._icloud_hour_start = time.time()
            self._icloud_calls_this_hour = 0
            self._icloud_initialized = True
            return api

        except Exception as exc:
            log.warning("Failed to initialize PyiCloudService: %s", exc)
            return None

    def _icloud_should_poll(self) -> bool:
        now = time.time()
        if now - self._icloud_hour_start >= 3600:
            self._icloud_calls_this_hour = 0
            self._icloud_hour_start = now
        if self._icloud_calls_this_hour >= MAX_ICLOUD_CALLS_PER_HOUR:
            return False
        if now - self._icloud_last_poll < ICLOUD_POLL_INTERVAL_SEC:
            return False
        return True

    def _poll_icloud(self) -> dict | None:
        api = self._get_api()
        if api is None:
            return None

        try:
            devices = []
            for d in api.devices:
                loc = d.location
                if loc:
                    devices.append({
                        "name": str(d),
                        "lat": loc.get("latitude"),
                        "lon": loc.get("longitude"),
                        "accuracy": loc.get("horizontalAccuracy"),
                        "is_iphone": "iPhone" in str(d),
                    })

            if not devices:
                return None

            iphone = [d for d in devices if d["is_iphone"]]
            target = iphone[0] if iphone else devices[0]

            target["source"] = "pyicloud"
            target["devices_found"] = len(devices)

            self._icloud_calls_this_hour += 1
            self._icloud_last_poll = time.time()
            return target

        except Exception as exc:
            log.warning("iCloud poll error: %s", exc)
            if "auth" in str(exc).lower() or "421" in str(exc) or "450" in str(exc):
                log.info("Resetting pyicloud instance after auth error")
                self._api = None
            return None

    # ── Geocoding (kept for city/province) ─────────────────────────────

    def _geocode(self, lat: float, lon: float) -> dict:
        import subprocess
        try:
            result = subprocess.run(
                [
                    "curl", "-s",
                    f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&zoom=10",
                    "-H", f"User-Agent: Helios-v6/3.1 ({os.environ.get('HELIOS_CONTACT_EMAIL', 'helios@localhost')})",
                    "--connect-timeout", "5", "--max-time", "10",
                ],
                capture_output=True, text=True, timeout=15,
            )
            data = json.loads(result.stdout)
            address = data.get("address", {})
            return {
                "city": address.get("city") or address.get("town") or address.get("municipality") or "Unknown",
                "province": address.get("state") or address.get("region") or "Unknown",
            }
        except Exception:
            return {"city": "Unknown", "province": "Unknown"}

    # ── Output ───────────────────────────────────────────────────────────

    def _write_location(self, data: dict) -> dict:
        """Write location data and return a privacy-safe event for consumers."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        lat = data.get("lat")
        lon = data.get("lon")
        zone = data.get("zone", "away")

        output = {
            "source": data.get("source", "fallback"),
            "ts": datetime.now(timezone.utc).isoformat(),
            "city": DEFAULT_CITY,
            "province": DEFAULT_PROVINCE,
            "zone": zone,
            "zone_state": self._last_zone or "",
            "zone_transition": "",
            "from_zone": "",
            "to_zone": "",
        }

        if lat and lon:
            output["lat"] = lat
            output["lon"] = lon
            output["accuracy"] = data.get("accuracy")
            output["device"] = data.get("name", "unknown")

            geo = self._geocode(lat, lon)
            output["city"] = geo["city"]
            output["province"] = geo["province"]

            self._last_lat = lat
            self._last_lon = lon

            # Zone transition detection
            prev_zone = self._last_zone
            self._last_zone = self._zone_resolver.resolve_zone(lat, lon)
            transition = self._zone_resolver.check_zone_transition(lat, lon, prev_zone)

            if transition:
                # Write sanitized transition to timeline
                safe_transition = sanitize_location_event(transition)
                self._write_timeline_event(safe_transition)
                # Surface transition for engine dispatch
                # Use or "" to coerce None values (prev_zone=None case)
                output["zone_transition"] = transition.get("transition") or ""
                output["from_zone"] = transition.get("from_zone") or ""
                output["to_zone"] = transition.get("to_zone") or ""

            # Always include current zone state in output
            output["zone_state"] = self._last_zone or ""

            # Dwell detection (POI lookup)
            visit = self._zone_resolver.update_dwell(lat, lon)
            if visit:
                # Sanitize before any external use
                safe_visit = sanitize_location_event(visit)
                output["place_name"] = safe_visit.get("place_name")
                output["place_type"] = safe_visit.get("place_type")
                output["confidence"] = safe_visit.get("confidence")
                output["poi_source"] = safe_visit.get("poi_source")
                self._write_timeline_event(safe_visit)

        elif data.get("error"):
            output["error"] = data["error"]

        LOCATION_FILE.write_text(json.dumps(output, indent=2))

        # Append raw coords to private local history (explicitly OK)
        if lat and lon:
            entry = {
                "ts": output["ts"],
                "lat": lat,
                "lon": lon,
                "accuracy": data.get("accuracy"),
                "city": output["city"],
                "province": output["province"],
                "source": data.get("source", "unknown"),
            }
            if output.get("place_name"):
                entry["place_name"] = output["place_name"]
                entry["place_type"] = output["place_type"]
            with open(LOCATION_HISTORY, "a") as f:
                f.write(json.dumps(entry) + "\n")

        # Privacy-safe log (no raw coords)
        source_label = data.get("source", "?")
        if source_label == "home_assistant":
            log.info("HA zone=%s", zone)
        elif source_label == "pyicloud":
            log.info("iCloud fallback active")
        elif source_label == "cached":
            log.debug("Using cached position")

        return output

    def _write_timeline_event(self, event: dict) -> None:
        """Write a privacy-safe event to the timeline database.

        Uses the canonical schema from migration 018:
        (ts, event_type, source_module, importance, summary, metadata, date_key)
        """
        if not self.db_path:
            return
        import sqlite3
        from .privacy_sanitizer import redact_sensitive_keys

        # Strip any raw coordinates before writing to DB
        safe_event = redact_sensitive_keys(event)

        # Determine event classification
        event_type = safe_event.get("event_type", "location_change")
        transition = safe_event.get("transition", "")
        from_zone = safe_event.get("from_zone", "")
        to_zone = safe_event.get("to_zone", "")
        place_name = safe_event.get("place_name", "")

        # Build a human-readable summary
        if transition and from_zone and to_zone:
            summary = f"Zone transition: {from_zone} → {to_zone}"
        elif place_name:
            summary = f"Visiting {place_name} ({safe_event.get('place_type', 'unknown')})"
        else:
            summary = f"Location event: {safe_event.get('zone', 'unknown zone')}"

        importance = 0.7 if transition else 0.5
        ts = safe_event.get("ts") or datetime.now(timezone.utc).isoformat()
        date_key = (datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    - timedelta(hours=6)).strftime("%Y-%m-%d") if "T" in ts else ts[:10]

        metadata = {k: v for k, v in safe_event.items()
                    if k not in ("event_type", "transition", "from_zone", "to_zone",
                                  "zone", "place_name", "place_type", "ts")}

        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """INSERT INTO timeline_events
                   (ts, event_type, source_module, importance, summary, metadata, date_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    event_type,
                    "location",
                    importance,
                    summary,
                    json.dumps(metadata) if metadata else None,
                    date_key,
                ),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            log.warning("Failed to write timeline event: %s", exc)

    # ── Main tick ───────────────────────────────────────────────────────

    def tick(self) -> dict[str, Any]:
        location_data: dict | None = None

        # Strategy 1: Home Assistant
        if self._ha_should_poll():
            data = self._poll_ha()
            if data and data.get("lat"):
                location_data = data
                self._ha_available = True

        # HA recovery
        if not self._ha_available and self._ha_token:
            data = self._poll_ha()
            if data and data.get("lat"):
                location_data = data
                self._ha_available = True
                log.info("HA recovered")

        # Strategy 2: iCloud fallback
        if location_data is None and self._ha_available is False:
            self._icloud_failures += 1
            if self._icloud_failures >= 3:
                log.info("HA unavailable for %d ticks — iCloud fallback", self._icloud_failures)

            if not self._icloud_initialized:
                self._get_api()

            if self._icloud_should_poll():
                data = self._poll_icloud()
                if data and data.get("lat"):
                    location_data = data

        # Strategy 3: Cached
        if location_data is None and self._last_lat is not None:
            location_data = {
                "lat": self._last_lat,
                "lon": self._last_lon,
                "source": "cached",
                "name": "Last known",
            }

        # Write and build result
        if location_data:
            result = self._write_location(location_data)
        else:
            log.debug("No location source available")
            result = {"source": "none", "province": DEFAULT_PROVINCE, "city": DEFAULT_CITY}

        # Return privacy-safe state for module consumers
        safe_result = sanitize_location_event(result)
        return safe_result

    def close(self) -> None:
        self._api = None
        self._icloud_initialized = False
