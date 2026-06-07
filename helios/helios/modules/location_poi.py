"""Helios v6 — POI provider and teachable cache layer.

Handles:
  - Overpass API queries with confidence scoring
  - Teachable local cache (poi_memory.json)
  - Configurable thresholds and provider selection
"""
import json
import logging
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("helios.location_poi")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
POI_FILE = DATA_DIR / "poi_memory.json"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Confidence scoring weights
CONFIDENCE_BRAND_MATCH = 0.30
CONFIDENCE_DISTANCE_PENALTY = 0.20
CONFIDENCE_TYPE_RELEVANCE = 0.25
CONFIDENCE_NAME_LENGTH = 0.15
CONFIDENCE_OSM_TAG_DEPTH = 0.10

# Distance tiers for penalty (meters)
DISTANCE_TIERS = [
    (30, 1.0),   # 0-30m = full confidence
    (75, 0.85),  # 30-75m
    (150, 0.70), # 75-150m
    (300, 0.50), # 150-300m
    (float("inf"), 0.30),
]


class POIProvider:
    """Encapsulates all POI lookup logic with confidence scoring."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.enabled = self.config.get("poi_lookup_enabled", False)
        self.provider = self.config.get("poi_provider", "overpass")
        self.min_dwell = self.config.get("poi_min_dwell_seconds", 600)
        self._memory: dict[str, dict] = {}
        self._memory_loaded = False

    # ── Memory ──────────────────────────────────────────────────────────

    def _load_memory(self) -> None:
        if self._memory_loaded:
            return
        if POI_FILE.exists():
            try:
                raw = json.loads(POI_FILE.read_text())
                self._memory = {
                    k: v for k, v in raw.items()
                    if isinstance(v, dict) and "name" in v
                }
            except Exception:
                self._memory = {}
        self._memory_loaded = True

    def _save_memory(self) -> None:
        POI_FILE.write_text(
            json.dumps(self._memory, indent=2, ensure_ascii=False)
        )

    def _grid_key(self, lat: float, lon: float, precision: int = 4) -> str:
        """Round to `precision` decimals for ~11m grid matching at 4dp."""
        return f"{lat:.{precision}f},{lon:.{precision}f}"

    def memory_lookup(self, lat: float, lon: float) -> dict | None:
        """Fast local cache hit — no network call."""
        self._load_memory()
        key = self._grid_key(lat, lon)
        entry = self._memory.get(key)
        if entry:
            return {
                "place_name": entry["name"],
                "place_type": entry.get("type", "unknown"),
                "confidence": 1.0,
                "source": "memory",
            }
        return None

    def memory_set(self, lat: float, lon: float, name: str,
                   place_type: str, brand: str = "") -> None:
        """Cache a confirmed POI for future visits."""
        key = self._grid_key(lat, lon)
        self._memory[key] = {
            "name": name,
            "type": place_type,
            "brand": brand,
            "lat": lat,
            "lon": lon,
            "first_seen": datetime.now(timezone.utc).isoformat(),
        }
        self._save_memory()

    # ── Overpass ────────────────────────────────────────────────────────

    def query_overpass(self, lat: float, lon: float,
                       radius_m: int = 150) -> list[dict]:
        """Query OSM Overpass for named amenities/shops near (lat, lon)."""
        if self.provider != "overpass":
            return []

        raw_query = (
            f'[out:json][timeout:10];'
            f'('
            f'node["name"]["amenity"](around:{radius_m},{lat},{lon});'
            f'way["name"]["amenity"](around:{radius_m},{lat},{lon});'
            f'node["name"]["shop"](around:{radius_m},{lat},{lon});'
            f'way["name"]["shop"](around:{radius_m},{lat},{lon});'
            f'relation["name"]["amenity"](around:{radius_m},{lat},{lon});'
            f');'
            f'out body 15 center;'
        )
        encoded = urllib.parse.quote(raw_query)
        url = f"{OVERPASS_URL}?data={encoded}"

        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "*/*", "User-Agent": "Helios-v6/3.1"},
            )
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            elements = data.get("elements", [])
            results = []
            for el in elements:
                tags = el.get("tags", {})
                name = tags.get("name")
                if not name:
                    continue
                el_lat = el.get("lat") or el.get("center", {}).get("lat")
                el_lon = el.get("lon") or el.get("center", {}).get("lon")
                if el_lat is None or el_lon is None:
                    continue
                results.append({
                    "name": name,
                    "type": tags.get("amenity", tags.get("shop", "unknown")),
                    "brand": tags.get("brand", ""),
                    "cuisine": tags.get("cuisine", ""),
                    "opening_hours": tags.get("opening_hours", ""),
                    "lat": el_lat,
                    "lon": el_lon,
                })
            return results
        except Exception as exc:
            log.debug("Overpass query failed: %s", exc)
            return []

    # ── Confidence scoring ────────────────────────────────────────────

    def _score_poi(self, poi: dict, lat: float, lon: float) -> float:
        """Score a POI match 0.0–1.0 based on multiple signals."""
        score = 0.0

        # 1. Brand match (strong signal of correctness)
        brand = poi.get("brand", "")
        name = poi.get("name", "")
        if brand and brand.lower() in name.lower():
            score += CONFIDENCE_BRAND_MATCH

        # 2. Distance penalty
        dlat = (poi["lat"] - lat) * 111000
        dlon = (poi["lon"] - lon) * 111000 * 0.63  # cos(51°)
        dist = (dlat**2 + dlon**2) ** 0.5
        for threshold, factor in DISTANCE_TIERS:
            if dist <= threshold:
                score += CONFIDENCE_DISTANCE_PENALTY * factor
                break

        # 3. Type relevance (amenity > shop > unknown)
        ptype = poi.get("type", "unknown")
        if ptype in {"cafe", "restaurant", "fast_food", "bar", "pub",
                     "nightclub", "gym", "fitness_centre", "pharmacy",
                     "bank", "fuel", "supermarket"}:
            score += CONFIDENCE_TYPE_RELEVANCE
        elif ptype != "unknown":
            score += CONFIDENCE_TYPE_RELEVANCE * 0.6

        # 4. Name length (longer names are more specific)
        if len(name) >= 15:
            score += CONFIDENCE_NAME_LENGTH
        elif len(name) >= 8:
            score += CONFIDENCE_NAME_LENGTH * 0.6
        else:
            score += CONFIDENCE_NAME_LENGTH * 0.3

        # 5. OSM tag depth (brand/cuisine/opening_hours = more detailed)
        bonus = 0
        if poi.get("brand"):
            bonus += 1
        if poi.get("cuisine"):
            bonus += 1
        if poi.get("opening_hours"):
            bonus += 1
        score += CONFIDENCE_OSM_TAG_DEPTH * min(bonus / 3, 1.0)

        return round(min(score, 1.0), 3)

    # ── Main lookup ─────────────────────────────────────────────────────

    def lookup(self, lat: float, lon: float,
               dwell_seconds: float) -> dict | None:
        """Try to identify the POI at (lat, lon).

        Returns dict with keys: place_name, place_type, confidence, source
        or None if no match or lookup disabled.
        """
        if not self.enabled:
            return None
        if not lat or not lon:
            return None

        # 1. Memory (fastest, privacy-first)
        cached = self.memory_lookup(lat, lon)
        if cached:
            return cached

        # 2. Only do network lookup for real visits (not brief passes)
        if dwell_seconds < self.min_dwell:
            return None

        # 3. Overpass lookup
        candidates = self.query_overpass(lat, lon, radius_m=150)
        if not candidates:
            return None

        # Score all candidates and pick best
        scored = []
        for c in candidates:
            conf = self._score_poi(c, lat, lon)
            scored.append((conf, c))

        scored.sort(reverse=True)
        best_conf, best = scored[0]

        # Threshold: require >= 0.60 confidence to cache
        if best_conf < 0.60:
            log.debug("Best POI confidence %.2f below threshold — not caching", best_conf)
            return None

        # Cache for future
        self.memory_set(
            lat, lon,
            best["name"], best["type"], best.get("brand", "")
        )

        return {
            "place_name": best["name"],
            "place_type": best["type"],
            "confidence": best_conf,
            "source": "overpass",
        }
