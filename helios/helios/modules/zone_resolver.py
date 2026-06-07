"""Helios v6 — Location Zone Resolver.

Abstracts the GPS processing pipeline:
  raw GPS → zone resolution → dwell detection → optional POI → sanitized event

Keeps raw coordinates strictly inside this module; everything that leaves
is privacy-safe (zone labels, place names, no lat/lon).
"""
import logging
import time
from typing import Any

log = logging.getLogger("helios.zone_resolver")

# Dwell detection config
DWELL_RADIUS_M = 75
DWELL_MIN_DURATION_SEC = 600
DWELL_BUFFER_MAX = 30


class LocationZoneResolver:
    """Processes raw GPS samples into privacy-safe zone/visit events."""

    def __init__(self, poi_provider=None, config: dict | None = None):
        self.config = config or {}
        self.poi_provider = poi_provider
        self._dwell_buffer: list[tuple[float, float, float]] = []
        self._last_visit_key: str | None = None
        self._last_visit_ts: float = 0.0
        self._debounce_sec = self.config.get("poi_visit_debounce_seconds", 1800)
        # Load zones from config; no hardcoded personal coordinates.
        self._zones: list[dict[str, Any]] = []
        raw_zones = self.config.get("zones", {})
        for label, zcfg in raw_zones.items():
            if isinstance(zcfg, dict) and zcfg.get("lat") is not None and zcfg.get("lon") is not None:
                self._zones.append({
                    "label": label,
                    "lat": float(zcfg["lat"]),
                    "lon": float(zcfg["lon"]),
                    "radius_m": float(zcfg.get("radius_m", 200)),
                })

    # ── Zone resolution ───────────────────────────────────────────────

    def resolve_zone(self, lat: float, lon: float) -> str:
        """Return a zone label or 'away' if no zone matches."""
        for zone in self._zones:
            if self._distance_m(lat, lon, zone["lat"], zone["lon"]) <= zone["radius_m"]:
                return zone["label"]
        return "away"

    @staticmethod
    def _distance_m(lat1: float, lon1: float,
                    lat2: float, lon2: float) -> float:
        """Approximate Haversine distance in meters."""
        dlat = (lat1 - lat2) * 111000
        dlon = (lon1 - lon2) * 111000 * 0.63  # cos(51°)
        return (dlat**2 + dlon**2) ** 0.5

    # ── Dwell detection ───────────────────────────────────────────────

    def update_dwell(self, lat: float, lon: float) -> dict | None:
        """Add a GPS sample and return a visit dict if dwell detected.

        Returns None if still moving, not enough data, or debounced.
        The returned dict contains NO raw lat/lon — only zone/place info.
        """
        now = time.time()

        self._dwell_buffer.append((now, lat, lon))
        if len(self._dwell_buffer) > DWELL_BUFFER_MAX:
            self._dwell_buffer.pop(0)

        min_samples = DWELL_BUFFER_MAX // 2
        if len(self._dwell_buffer) < min_samples:
            return None

        # Centroid
        avg_lat = sum(p[1] for p in self._dwell_buffer) / len(self._dwell_buffer)
        avg_lon = sum(p[2] for p in self._dwell_buffer) / len(self._dwell_buffer)

        # Max radius
        max_dist = 0.0
        for _, p_lat, p_lon in self._dwell_buffer:
            dist = self._distance_m(p_lat, p_lon, avg_lat, avg_lon)
            if dist > max_dist:
                max_dist = dist

        if max_dist > DWELL_RADIUS_M:
            return None  # still moving

        # Dwell duration
        first_ts = self._dwell_buffer[0][0]
        dwell_sec = now - first_ts
        if dwell_sec < DWELL_MIN_DURATION_SEC:
            return None

        # Debounce
        visit_key = self._grid_key(avg_lat, avg_lon)
        if visit_key == self._last_visit_key and (now - self._last_visit_ts) < self._debounce_sec:
            return None

        self._last_visit_key = visit_key
        self._last_visit_ts = now

        # Resolve zone (uses raw coords internally, returns label only)
        zone = self.resolve_zone(avg_lat, avg_lon)

        # Optional POI lookup
        poi_result = None
        if self.poi_provider:
            poi_result = self.poi_provider.lookup(avg_lat, avg_lon, dwell_sec)

        # Build privacy-safe visit event
        event: dict[str, Any] = {
            "event_type": "visit",
            "zone": zone,
            "dwell_seconds": round(dwell_sec, 1),
            "ts": time.time(),
        }
        if poi_result:
            event["place_name"] = poi_result["place_name"]
            event["place_type"] = poi_result["place_type"]
            event["confidence"] = poi_result.get("confidence", 0.0)
            event["poi_source"] = poi_result.get("source", "unknown")
            log.info(
                "Visit: %s (%s) zone=%s confidence=%.2f",
                poi_result["place_name"],
                poi_result["place_type"],
                zone,
                poi_result.get("confidence", 0.0),
            )
        else:
            if zone == "away":
                log.info("Visit: away zone, no POI matched")
            else:
                log.debug("Visit: zone=%s, no POI (home/work zone)", zone)

        return event

    @staticmethod
    def _grid_key(lat: float, lon: float, precision: int = 4) -> str:
        return f"{lat:.{precision}f},{lon:.{precision}f}"

    def reset_dwell(self) -> None:
        """Clear the dwell buffer (e.g., on zone transition)."""
        self._dwell_buffer.clear()
        self._last_visit_key = None
        self._last_visit_ts = 0.0

    # ── Transition detection ────────────────────────────────────────────

    def check_zone_transition(self, lat: float, lon: float,
                               prev_zone: str | None) -> dict | None:
        """Detect not_home→home or home→away transitions.

        Returns a privacy-safe transition event or None.
        """
        zone = self.resolve_zone(lat, lon)
        if prev_zone is None:
            return {"event_type": "zone", "zone": zone, "transition": None}

        if prev_zone == "away" and zone == "home":
            self.reset_dwell()
            return {
                "event_type": "zone_transition",
                "from_zone": prev_zone,
                "to_zone": zone,
                "transition": "arrival",
            }
        if prev_zone == "home" and zone == "away":
            self.reset_dwell()
            return {
                "event_type": "zone_transition",
                "from_zone": prev_zone,
                "to_zone": zone,
                "transition": "departure",
            }
        return None
