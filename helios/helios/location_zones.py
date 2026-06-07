"""Helios — Location zone abstraction (privacy-safe, deterministic, no network).

Resolves raw GPS coordinates into abstract zone labels (home, worksite, clinic,
city_area, unknown) using haversine distance and configurable zone definitions.

This module does NOT import helios.modules.location and makes zero network calls
inside resolve() / daily_summary().  All zone lookups are pure computation from
in-memory config + the coordinates passed in.
"""

from __future__ import annotations

import json
import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("helios.location_zones")

# ── Default paths ───────────────────────────────────────────────────────

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
RUNTIME_ZONES_PATH = DATA_DIR / "location_zones.json"

# ── Haversine ───────────────────────────────────────────────────────────

_EARTH_RADIUS_KM = 6371.0088


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in km between two lat/lon points."""
    rlat1, rlon1 = math.radians(lat1), math.radians(lon1)
    rlat2, rlon2 = math.radians(lat2), math.radians(lon2)
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return _EARTH_RADIUS_KM * c


def _meters_to_km(m: float) -> float:
    return m / 1000.0


# ── Data models ─────────────────────────────────────────────────────────


@dataclass
class ZoneDefinition:
    """A known geographic zone with center, radius, and metadata."""

    zone_id: str
    label: str
    lat: float
    lon: float
    radius_m: float = 200.0
    privacy_level: str = "coarse"

    def contains(self, lat: float, lon: float) -> bool:
        return _haversine_km(self.lat, self.lon, lat, lon) <= _meters_to_km(self.radius_m)

@dataclass
class ZoneResult:
    """Result of resolving a single coordinate pair to a zone."""

    zone_id: str
    label: str
    confidence: float
    privacy_level: str = "coarse"

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "label": self.label,
            "confidence": round(self.confidence, 3),
            "privacy_level": self.privacy_level,
        }


# ── Default config (sentinel values — load from runtime config) ──────────

DEFAULT_ZONES: list[dict[str, Any]] = [
    {
        "zone_id": "home",
        "label": "Home",
        "lat": 0.0,
        "lon": 0.0,
        "radius_m": 200,
        "privacy_level": "coarse",
    },
    {
        "zone_id": "worksite_cluster",
        "label": "Worksite",
        "lat": 0.0,
        "lon": 0.0,
        "radius_m": 500,
        "privacy_level": "coarse",
    },
    {
        "zone_id": "clinic",
        "label": "Clinic",
        "lat": 0.0,
        "lon": 0.0,
        "radius_m": 300,
        "privacy_level": "coarse",
    },
]

DEFAULT_CONFIG: dict[str, Any] = {
    "home": {"lat": 0.0, "lon": 0.0, "radius_m": 200},
    "zones": DEFAULT_ZONES,
    "gap_threshold_minutes": 120,
    "city_area_radius_km": 50.0,
    "city_label": "City",
}


# ── Runtime config loader ────────────────────────────────────────────────


def load_zones_from_config(path: Path | None = None) -> dict[str, Any] | None:
    """Load zone configuration from a runtime JSON file.

    Reads from ``~/.hermes/helios/data/location_zones.json`` by default.

    Returns the parsed dict on success, or ``None`` if the file is missing
    or cannot be parsed.
    """
    config_path = path or RUNTIME_ZONES_PATH
    try:
        if not config_path.exists():
            return None
        with open(config_path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning("Runtime zones config is not a dict at %s", config_path)
            return None
        return data
    except (json.JSONDecodeError, PermissionError, OSError) as exc:
        log.warning("Failed to load runtime zones config from %s: %s", config_path, exc)
        return None


# ── Resolver ────────────────────────────────────────────────────────────


class LocationZoneResolver:
    """Deterministic, offline zone resolver.

    Config may include:
      - home: {lat, lon, radius_m}
      - zones: list of dicts matching DEFAULT_ZONES shape
      - gap_threshold_minutes: minutes of gap that counts as "major"
      - city_area_radius_km: radius for city_area classification
      - city_label: label for city_area zone

    No network calls happen inside resolve() or daily_summary().

    Config resolution order:
      1. Runtime config file (``~/.hermes/helios/data/location_zones.json``)
      2. Explicit ``config`` argument passed to constructor
      3. DEFAULT_CONFIG (sentinel 0.0, 0.0 coordinates)

    If the resolved home lat/lon are both 0.0 (sentinel), the resolver
    logs a warning and sets zones as empty — no zone matching can happen,
    which is the safe default.
    """

    def __init__(self, config: dict | None = None):
        # Config resolution: runtime file → explicit argument → DEFAULT_CONFIG
        runtime_cfg = load_zones_from_config()
        if runtime_cfg:
            cfg = {**DEFAULT_CONFIG, **runtime_cfg}
            if config:
                cfg = {**cfg, **config}
        elif config:
            cfg = {**DEFAULT_CONFIG, **config}
        else:
            cfg = dict(DEFAULT_CONFIG)

        self._home_lat: float = cfg["home"]["lat"]
        self._home_lon: float = cfg["home"]["lon"]
        self._home_radius_m: float = cfg["home"].get("radius_m", 200)
        self._gap_threshold_minutes: int = cfg.get("gap_threshold_minutes", 120)
        self._city_area_radius_km: float = cfg.get("city_area_radius_km", 50.0)
        self._city_label: str = cfg.get("city_label", "City")
        self._data_dir: Path = Path(cfg.get("data_dir", str(DATA_DIR)))

        # Build zone list
        zone_defs = cfg.get("zones", DEFAULT_ZONES)

        # If home coords are sentinel (0.0, 0.0), warn and use empty zones
        if self._home_lat == 0.0 and self._home_lon == 0.0:
            log.warning(
                "Home coordinates are (0.0, 0.0) — sentinel values. "
                "Zone matching disabled. Load real coordinates from "
                "~/.hermes/helios/data/location_zones.json or pass explicit config."
            )

        self._zones: list[ZoneDefinition] = []
        for zd in zone_defs:
            zone_lat = zd["lat"]
            zone_lon = zd["lon"]
            # Skip zones with sentinel coordinates
            if zone_lat == 0.0 and zone_lon == 0.0:
                log.debug(
                    "Skipping zone '%s' with sentinel coordinates (0.0, 0.0).",
                    zd.get("zone_id", "unknown"),
                )
                continue
            self._zones.append(ZoneDefinition(
                zone_id=zd["zone_id"],
                label=zd.get("label", zd["zone_id"]),
                lat=zone_lat,
                lon=zone_lon,
                radius_m=zd.get("radius_m", 200),
                privacy_level=zd.get("privacy_level", "coarse"),
            ))

    # ── Public API ──────────────────────────────────────────────────

    def resolve(self, lat: float, lon: float) -> dict:
        """Resolve a single lat/lon to a zone dict.

        Returns {"zone_id": str, "label": str, "confidence": float, "privacy_level": "coarse"}.
        Deterministic — same coords always produce the same zone.
        No network calls.
        """
        result = self._resolve_result(lat, lon)
        return result.to_dict()

    def daily_summary(self, samples: list[dict], date_str: str) -> dict:
        """Generate a daily location summary from a list of sample dicts.

        Each sample must have keys: "ts" (ISO string), "lat" (float), "lon" (float).
        Optional: "accuracy" (float), "source" (str).

        Returns a dict with:
          - date: str
          - first_seen: str (ISO)
          - last_seen: str (ISO)
          - zones_visited: list of dicts with zone info + time ranges
          - major_gaps: list of gap dicts (start, end, duration_minutes)
          - confidence: float
          - narrative: str (privacy-safe, no raw coords)
        """
        if not samples:
            return {
                "date": date_str,
                "first_seen": None,
                "last_seen": None,
                "zones_visited": [],
                "major_gaps": [],
                "confidence": 0.0,
                "narrative": f"No location data for {date_str}.",
            }

        # Parse and sort samples by timestamp
        parsed: list[dict[str, Any]] = []
        for s in samples:
            ts_raw = s.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            parsed.append({
                "ts": ts,
                "lat": s.get("lat", 0.0),
                "lon": s.get("lon", 0.0),
                "accuracy": s.get("accuracy"),
                "source": s.get("source", "unknown"),
            })

        if not parsed:
            return {
                "date": date_str,
                "first_seen": None,
                "last_seen": None,
                "zones_visited": [],
                "major_gaps": [],
                "confidence": 0.0,
                "narrative": f"No valid timestamps for {date_str}.",
            }

        parsed.sort(key=lambda s: s["ts"])

        first_seen = parsed[0]["ts"].isoformat()
        last_seen = parsed[-1]["ts"].isoformat()

        # Resolve each sample to a zone
        for s in parsed:
            zone_result = self._resolve_result(s["lat"], s["lon"])
            s["zone_id"] = zone_result.zone_id
            s["zone_label"] = zone_result.label

        # Build zones_visited with time ranges
        zones_visited: list[dict[str, Any]] = []

        def _add_zone_entry(zone_id: str, zone_label: str, start_ts: datetime, end_ts: datetime,
                            sample_count: int) -> None:
            zones_visited.append({
                "zone_id": zone_id,
                "label": zone_label,
                "start": start_ts.isoformat(),
                "end": end_ts.isoformat(),
                "sample_count": sample_count,
            })

        current_zone = parsed[0]["zone_id"]
        current_label = parsed[0]["zone_label"]
        zone_start = parsed[0]["ts"]
        zone_count = 1

        for i in range(1, len(parsed)):
            if parsed[i]["zone_id"] == current_zone:
                zone_count += 1
                continue
            # Zone changed — flush previous zone
            _add_zone_entry(current_zone, current_label, zone_start, parsed[i - 1]["ts"], zone_count)
            current_zone = parsed[i]["zone_id"]
            current_label = parsed[i]["zone_label"]
            zone_start = parsed[i]["ts"]
            zone_count = 1

        # Flush last zone
        _add_zone_entry(current_zone, current_label, zone_start, parsed[-1]["ts"], zone_count)

        # Detect major gaps
        gap_threshold_min = self._gap_threshold_minutes
        major_gaps: list[dict[str, Any]] = []

        for i in range(1, len(parsed)):
            delta = (parsed[i]["ts"] - parsed[i - 1]["ts"]).total_seconds() / 60.0
            if delta >= gap_threshold_min:
                major_gaps.append({
                    "start": parsed[i - 1]["ts"].isoformat(),
                    "end": parsed[i]["ts"].isoformat(),
                    "duration_minutes": round(delta, 1),
                })

        # Confidence based on sample count and coverage
        confidence = self._compute_confidence(parsed, date_str)

        # Narrative (privacy-safe: zone labels only, no raw coords)
        narrative = self._build_narrative(date_str, zones_visited, major_gaps)

        summary: dict[str, Any] = {
            "date": date_str,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "zones_visited": zones_visited,
            "major_gaps": major_gaps,
            "confidence": round(confidence, 3),
            "narrative": narrative,
        }

        # Persist to disk
        self._save_summary(summary, date_str)

        return summary

    # ── Internals ───────────────────────────────────────────────────

    def _resolve_result(self, lat: float, lon: float) -> ZoneResult:
        """Determine zone for a point. No network calls."""
        # Check known zones in order (first match wins)
        for z in self._zones:
            if z.contains(lat, lon):
                # Confidence based on distance from zone center
                dist_km = _haversine_km(lat, lon, z.lat, z.lon)
                max_km = _meters_to_km(z.radius_m)
                # Higher confidence when closer to center
                if max_km > 0:
                    ratio = max(0.0, 1.0 - (dist_km / max_km))
                    confidence = 0.7 + 0.3 * ratio  # range 0.7–1.0 inside zone
                else:
                    confidence = 0.7
                return ZoneResult(
                    zone_id=z.zone_id,
                    label=z.label,
                    confidence=confidence,
                    privacy_level=z.privacy_level,
                )

        # Not in any known zone — check if in city area
        if self._city_area_radius_km > 0 and not (self._home_lat == 0.0 and self._home_lon == 0.0):
            dist_from_home_km = _haversine_km(lat, lon, self._home_lat, self._home_lon)
            if dist_from_home_km <= self._city_area_radius_km:
                return ZoneResult(
                    zone_id="city_area",
                    label=f"{self._city_label} area",
                    confidence=0.3,
                    privacy_level="coarse",
                )

        # Completely unknown
        return ZoneResult(
            zone_id="unknown",
            label="Unknown location",
            confidence=0.1,
            privacy_level="coarse",
        )

    def _compute_confidence(self, parsed: list[dict[str, Any]], date_str: str) -> float:
        """Compute confidence for a daily summary based on data quality."""
        if not parsed:
            return 0.0

        # Base: sample count gives diminishing returns
        sample_score = min(1.0, len(parsed) / 60.0)

        # Time span: more coverage = higher score
        span_hours = (parsed[-1]["ts"] - parsed[0]["ts"]).total_seconds() / 3600.0
        span_score = min(1.0, span_hours / 14.0)  # full day ≈ 14 waking hours

        # Zone certainty: fraction of samples in known zones (not "unknown")
        known_count = sum(1 for s in parsed if s.get("zone_id", "unknown") != "unknown")
        known_score = known_count / len(parsed) if parsed else 0.0

        return 0.3 * sample_score + 0.4 * span_score + 0.3 * known_score

    def _build_narrative(
        self,
        date_str: str,
        zones_visited: list[dict[str, Any]],
        major_gaps: list[dict[str, Any]],
    ) -> str:
        """Build a privacy-safe narrative line for a daily summary.

        Uses zone labels only — no raw coordinates.
        """
        if not zones_visited:
            return f"No movement data for {date_str}."

        parts: list[str] = []
        for z in zones_visited:
            start_short = z["start"].split("T")[1][:5] if "T" in z["start"] else "?"
            end_short = z["end"].split("T")[1][:5] if "T" in z["end"] else "?"
            parts.append(f"{z['label']} ({start_short}-{end_short})")

        narrative = f"On {date_str}: " + ", then ".join(parts) + "."

        if major_gaps:
            gap_desc = ", ".join(
                f"{g['duration_minutes']:.0f} min gap" for g in major_gaps
            )
            narrative += f" Notable gaps: {gap_desc}."

        return narrative

    def _save_summary(self, summary: dict, date_str: str) -> None:
        """Persist summary JSON to the data directory."""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            path = self._data_dir / f"location_daily_summary_{date_str}.json"
            path.write_text(json.dumps(summary, indent=2, default=str))
        except Exception as exc:
            log.warning("Failed to save daily summary for %s: %s", date_str, exc)