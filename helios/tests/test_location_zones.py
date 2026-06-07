"""Tests for helios.location_zones — Epic 2: Location zone abstraction.

Key invariants verified:
  - Raw coordinates are NEVER in summary text or narrative output
  - Gaps over threshold are explicitly reported
  - Home / work / unknown zones are deterministic from sample fixtures
  - LocationZoneResolver works without network calls
  - Unknown locations get "unknown" zone_id
  - Narrative line is privacy-safe (city/zone labels only)
  - Sentinel coordinates (0.0, 0.0) produce empty zones with warnings
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helios.location_zones import LocationZoneResolver, ZoneDefinition, _haversine_km


# ── Test config (explicit coordinates, not relying on DEFAULT_ZONES) ────

TEST_HOME_LAT = 51.1650
TEST_HOME_LON = -113.9612
TEST_WORKSITE_LAT = 50.9530
TEST_WORKSITE_LON = -114.1050
TEST_CLINIC_LAT = 51.0780
TEST_CLINIC_LON = -114.1300

TEST_CONFIG = {
    "home": {"lat": TEST_HOME_LAT, "lon": TEST_HOME_LON, "radius_m": 200},
    "zones": [
        {
            "zone_id": "home",
            "label": "Home",
            "lat": TEST_HOME_LAT,
            "lon": TEST_HOME_LON,
            "radius_m": 200,
            "privacy_level": "coarse",
        },
        {
            "zone_id": "worksite_cluster",
            "label": "Worksite",
            "lat": TEST_WORKSITE_LAT,
            "lon": TEST_WORKSITE_LON,
            "radius_m": 500,
            "privacy_level": "coarse",
        },
        {
            "zone_id": "clinic",
            "label": "Clinic",
            "lat": TEST_CLINIC_LAT,
            "lon": TEST_CLINIC_LON,
            "radius_m": 300,
            "privacy_level": "coarse",
        },
    ],
    "gap_threshold_minutes": 120,
    "city_area_radius_km": 50.0,
    "city_label": "Anytown",
}


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def home_coords() -> tuple[float, float]:
    """Home coordinates for test config."""
    return (TEST_HOME_LAT, TEST_HOME_LON)


@pytest.fixture
def worksite_coords() -> tuple[float, float]:
    """Worksite coordinates for test config."""
    return (TEST_WORKSITE_LAT, TEST_WORKSITE_LON)


@pytest.fixture
def resolver() -> LocationZoneResolver:
    """A resolver with explicit test config."""
    return LocationZoneResolver(config=TEST_CONFIG)


@pytest.fixture
def resolver_with_data_dir(tmp_path: Path) -> LocationZoneResolver:
    """A resolver that writes summaries to a temp data dir."""
    config = {**TEST_CONFIG, "data_dir": str(tmp_path)}
    return LocationZoneResolver(config=config)


# ── Task 2.1: LocationZoneResolver.resolve() ────────────────────────────


class TestResolve:
    """Tests for the resolve() method."""

    def test_home_zone_deterministic(self, resolver: LocationZoneResolver):
        """Coordinates near home always resolve to 'home' zone."""
        result = resolver.resolve(TEST_HOME_LAT, TEST_HOME_LON)
        assert result["zone_id"] == "home"
        assert result["label"] == "Home"
        assert result["confidence"] >= 0.7

    def test_worksite_zone_deterministic(self, resolver: LocationZoneResolver):
        """Coordinates at worksite resolve to 'worksite_cluster'."""
        result = resolver.resolve(TEST_WORKSITE_LAT, TEST_WORKSITE_LON)
        assert result["zone_id"] == "worksite_cluster"
        assert result["label"] == "Worksite"

    def test_clinic_zone(self, resolver: LocationZoneResolver):
        """Coordinates at clinic resolve to 'clinic'."""
        result = resolver.resolve(TEST_CLINIC_LAT, TEST_CLINIC_LON)
        assert result["zone_id"] == "clinic"

    def test_unknown_far_away(self, resolver: LocationZoneResolver):
        """Coordinates far from all known zones get 'unknown' zone_id."""
        # Honolulu, roughly 5000 km away
        result = resolver.resolve(21.3069, -157.8583)
        assert result["zone_id"] == "unknown"
        assert result["label"] == "Unknown location"

    def test_city_area_within_radius(self, resolver: LocationZoneResolver):
        """Coordinates not in any known zone but within city area get city_area."""
        # ~10 km from home — not in any zone, but within 50 km city radius
        result = resolver.resolve(51.22, -113.85)
        assert result["zone_id"] == "city_area"
        assert result.get("city_label")["label"]

    def test_no_network_calls(self, resolver: LocationZoneResolver):
        """resolve() must be pure computation — no network or I/O side-effects."""
        # Simply calling resolve should not raise or require any network
        for lat, lon in [(TEST_HOME_LAT, TEST_HOME_LON), (0.0, 0.0), (90.0, 180.0)]:
            result = resolver.resolve(lat, lon)
            assert "zone_id" in result
            assert "confidence" in result

    def test_privacy_level_always_coarse(self, resolver: LocationZoneResolver):
        """All results use privacy_level='coarse' by default."""
        for lat, lon in [(TEST_HOME_LAT, TEST_HOME_LON), (21.3069, -157.8583)]:
            result = resolver.resolve(lat, lon)
            assert result["privacy_level"] == "coarse"

    def test_confidence_higher_at_center(self, resolver: LocationZoneResolver):
        """Confidence is higher at the center of a zone than at the edge."""
        center_result = resolver.resolve(TEST_HOME_LAT, TEST_HOME_LON)  # center of home
        edge_result = resolver.resolve(51.1670, -113.9650)  # near edge of home radius
        assert center_result["confidence"] >= edge_result["confidence"]

    def test_custom_zones(self):
        """Custom zone definitions are respected."""
        config = {
            "home": {"lat": 51.08, "lon": -114.15, "radius_m": 100},
            "zones": [
                {
                    "zone_id": "gym",
                    "label": "Gym",
                    "lat": 51.08,
                    "lon": -114.15,
                    "radius_m": 100,
                },
            ],
        }
        r = LocationZoneResolver(config=config)
        result = r.resolve(51.08, -114.15)
        assert result["zone_id"] == "gym"
        assert result["label"] == "Gym"


# ── Sentinel coordinates (0.0, 0.0) ────────────────────────────────────


class TestSentinelCoordinates:
    """Tests for sentinel (0.0, 0.0) coordinate handling."""

    def test_default_resolver_has_no_zones(self):
        """Resolver with no config has empty zones (sentinel coordinates)."""
        r = LocationZoneResolver()
        assert len(r._zones) == 0

    def test_default_resolver_resolves_to_unknown(self):
        """Resolver with sentinel coordinates resolves everything to unknown."""
        r = LocationZoneResolver()
        result = r.resolve(51.1650, -113.9612)
        assert result["zone_id"] == "unknown"

    def test_default_resolver_city_area_disabled(self):
        """With sentinel home coords, city_area classification is disabled."""
        r = LocationZoneResolver()
        result = r.resolve(51.22, -113.85)
        assert result["zone_id"] == "unknown"


# ── Task 2.2: daily_summary() ───────────────────────────────────────────


class TestDailySummary:
    """Tests for the daily_summary() method."""

    def _make_samples(
        self,
        rows: list[tuple[str, float, float]],
    ) -> list[dict]:
        """Helper: build sample dicts from (ts, lat, lon) tuples."""
        return [
            {"ts": ts, "lat": lat, "lon": lon, "source": "test"}
            for ts, lat, lon in rows
        ]

    def test_basic_daily_summary(
        self, resolver_with_data_dir: LocationZoneResolver
    ):
        """daily_summary produces required keys from sample data."""
        samples = self._make_samples([
            ("2026-05-22T06:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),  # home
            ("2026-05-22T08:00:00+00:00", TEST_WORKSITE_LAT, TEST_WORKSITE_LON),  # worksite
            ("2026-05-22T16:00:00+00:00", TEST_WORKSITE_LAT, TEST_WORKSITE_LON),  # worksite
            ("2026-05-22T18:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),  # home
        ])

        result = resolver_with_data_dir.daily_summary(samples, "2026-05-22")

        assert result["date"] == "2026-05-22"
        assert result["first_seen"] is not None
        assert result["last_seen"] is not None
        assert isinstance(result["zones_visited"], list)
        assert isinstance(result["major_gaps"], list)
        assert isinstance(result["confidence"], float)
        assert isinstance(result["narrative"], str)

    def test_daily_summary_zones_visited(
        self, resolver_with_data_dir: LocationZoneResolver
    ):
        """zones_visited contains home and work zone entries with time ranges."""
        samples = self._make_samples([
            ("2026-05-22T06:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),  # home 6am
            ("2026-05-22T13:00:00+00:00", TEST_WORKSITE_LAT, TEST_WORKSITE_LON),  # work 1pm
            ("2026-05-22T20:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),  # home 8pm
        ])

        result = resolver_with_data_dir.daily_summary(samples, "2026-05-22")

        zone_ids = [z["zone_id"] for z in result["zones_visited"]]
        assert "home" in zone_ids
        assert "worksite_cluster" in zone_ids

        # Each zone entry has time range
        for z in result["zones_visited"]:
            assert "start" in z
            assert "end" in z
            assert "sample_count" in z
            assert z["sample_count"] >= 1

    def test_major_gaps_reported(
        self, resolver_with_data_dir: LocationZoneResolver
    ):
        """Gaps over the threshold are explicitly reported."""
        # 6 hours between samples — well over 120-minute default threshold
        samples = self._make_samples([
            ("2026-05-22T06:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),
            ("2026-05-22T12:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),  # 6h gap
        ])

        result = resolver_with_data_dir.daily_summary(samples, "2026-05-22")

        assert len(result["major_gaps"]) >= 1
        gap = result["major_gaps"][0]
        assert gap["duration_minutes"] >= 120
        assert "start" in gap
        assert "end" in gap

    def test_no_gaps_below_threshold(
        self, resolver_with_data_dir: LocationZoneResolver
    ):
        """Samples close together should NOT produce major gaps."""
        samples = self._make_samples([
            ("2026-05-22T06:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),
            ("2026-05-22T06:30:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),
            ("2026-05-22T07:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),
        ])

        result = resolver_with_data_dir.daily_summary(samples, "2026-05-22")
        assert result["major_gaps"] == []

    def test_empty_samples_produce_empty_summary(
        self, resolver_with_data_dir: LocationZoneResolver
    ):
        """Empty sample list results in zero-confidence, no-data summary."""
        result = resolver_with_data_dir.daily_summary([], "2026-05-22")

        assert result["date"] == "2026-05-22"
        assert result["first_seen"] is None
        assert result["last_seen"] is None
        assert result["zones_visited"] == []
        assert result["major_gaps"] == []
        assert result["confidence"] == 0.0

    def test_summary_saved_to_disk(
        self, tmp_path: Path, resolver_with_data_dir: LocationZoneResolver
    ):
        """daily_summary persists results to a JSON file."""
        samples = self._make_samples([
            ("2026-05-22T06:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),
            ("2026-05-22T12:00:00+00:00", TEST_WORKSITE_LAT, TEST_WORKSITE_LON),
        ])
        data_dir = tmp_path / "data"
        config = {**TEST_CONFIG, "data_dir": str(data_dir)}
        r = LocationZoneResolver(config=config)
        result = r.daily_summary(samples, "2026-05-22")

        saved_path = data_dir / "location_daily_summary_2026-05-22.json"
        assert saved_path.exists()

        loaded = json.loads(saved_path.read_text())
        assert loaded["date"] == "2026-05-22"
        assert len(loaded["zones_visited"]) > 0


# ── Privacy invariants ──────────────────────────────────────────────────


class TestPrivacyInvariants:
    """Raw coordinates must NEVER appear in summary text or narrative."""

    def _make_samples(
        self,
        rows: list[tuple[str, float, float]],
    ) -> list[dict]:
        return [
            {"ts": ts, "lat": lat, "lon": lon, "source": "test"}
            for ts, lat, lon in rows
        ]

    def test_raw_coords_not_in_narrative(
        self, resolver_with_data_dir: LocationZoneResolver
    ):
        """Narrative line contains zone/city labels only — no lat/lon floats."""
        samples = self._make_samples([
            ("2026-05-22T06:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),
            ("2026-05-22T13:00:00+00:00", TEST_WORKSITE_LAT, TEST_WORKSITE_LON),
            ("2026-05-22T20:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),
        ])

        result = resolver_with_data_dir.daily_summary(samples, "2026-05-22")
        narrative = result["narrative"]

        # Raw lat/lon values must not appear in narrative
        assert "51.1650" not in narrative
        assert "-113.9612" not in narrative
        assert "50.9530" not in narrative
        assert "-114.1050" not in narrative

        # But zone labels SHOULD be present
        assert "Home" in narrative or "Worksite" in narrative

    def test_raw_coords_not_in_zones_visited_labels(
        self, resolver_with_data_dir: LocationZoneResolver
    ):
        """zones_visited entries use labels, not coordinates."""
        samples = self._make_samples([
            ("2026-05-22T06:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),
            ("2026-05-22T20:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),
        ])

        result = resolver_with_data_dir.daily_summary(samples, "2026-05-22")

        for z in result["zones_visited"]:
            # Labels are strings like "Home", not coordinates
            assert "51." not in z["label"]
            assert "-113." not in z["label"]

    def test_raw_coords_not_in_resolve_result(self):
        """resolve() result dict never includes raw lat/lon under any key."""
        r = LocationZoneResolver(config=TEST_CONFIG)
        result = r.resolve(TEST_HOME_LAT, TEST_HOME_LON)

        assert "lat" not in result
        assert "lon" not in result
        assert "latitude" not in result
        assert "longitude" not in result
        assert "51.165" not in str(result)
        assert "-113.961" not in str(result)

    def test_narrative_is_privacy_safe(
        self, resolver_with_data_dir: LocationZoneResolver
    ):
        """Narrative references city/zone labels only."""
        samples = self._make_samples([
            ("2026-05-22T06:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),
            ("2026-05-22T08:00:00+00:00", TEST_WORKSITE_LAT, TEST_WORKSITE_LON),
            ("2026-05-22T16:00:00+00:00", TEST_WORKSITE_LAT, TEST_WORKSITE_LON),
            ("2026-05-22T18:00:00+00:00", TEST_HOME_LAT, TEST_HOME_LON),
        ])

        result = resolver_with_data_dir.daily_summary(samples, "2026-05-22")
        narrative = result["narrative"]

        # Narrative should start with "On 2026-05-22:"
        assert narrative.startswith("On 2026-05-22:")

        # Contains zone labels, not coordinates
        assert "Home" in narrative or "Worksite" in narrative
        for lat_str in ["51.1650", "-113.9612", "50.9530", "-114.1050"]:
            assert lat_str not in narrative


# ── Determinism ─────────────────────────────────────────────────────────


class TestDeterminism:
    """Zone resolution must be deterministic — same input, same output."""

    def test_resolve_deterministic(self):
        """Same coordinates always give same zone."""
        r = LocationZoneResolver(config=TEST_CONFIG)
        r1 = r.resolve(TEST_HOME_LAT, TEST_HOME_LON)
        r2 = r.resolve(TEST_HOME_LAT, TEST_HOME_LON)
        assert r1 == r2

    def test_worksite_deterministic(self):
        """Worksite coordinates always give worksite_cluster."""
        r = LocationZoneResolver(config=TEST_CONFIG)
        result = r.resolve(TEST_WORKSITE_LAT, TEST_WORKSITE_LON)
        assert result["zone_id"] == "worksite_cluster"
        # Second call same result
        result2 = r.resolve(TEST_WORKSITE_LAT, TEST_WORKSITE_LON)
        assert result == result2

    def test_unknown_deterministic(self):
        """Far-away coordinates always resolve to unknown."""
        r = LocationZoneResolver(config=TEST_CONFIG)
        r1 = r.resolve(21.3069, -157.8583)  # Honolulu
        r2 = r.resolve(21.3069, -157.8583)
        assert r1["zone_id"] == "unknown"
        assert r1 == r2


# ── Haversine utility ───────────────────────────────────────────────────


class TestHaversine:
    """Tests for the _haversine_km helper."""

    def test_zero_distance(self):
        """Same point → zero distance."""
        assert _haversine_km(TEST_HOME_LAT, TEST_HOME_LON, TEST_HOME_LAT, TEST_HOME_LON) == 0.0

    def test_known_distance(self):
        """Check approximate distance between home and worksite (~23-24 km)."""
        dist = _haversine_km(TEST_HOME_LAT, TEST_HOME_LON, TEST_WORKSITE_LAT, TEST_WORKSITE_LON)
        # Should be roughly 23-25 km
        assert 22.0 < dist < 26.0

    def test_short_distance(self):
        """200m-ish distances are detected correctly."""
        # ~0.002 deg ≈ ~200m
        dist = _haversine_km(TEST_HOME_LAT, TEST_HOME_LON, 51.1660, -113.9630)
        assert dist < 1.0  # less than 1 km


# ── No network calls ────────────────────────────────────────────────────


class TestNoNetwork:
    """LocationZoneResolver must work without any network calls."""

    def test_resolve_without_network(self):
        """resolve() requires no network — pure math."""
        r = LocationZoneResolver(config=TEST_CONFIG)
        # This should work in a completely offline environment
        for lat, lon in [(TEST_HOME_LAT, TEST_HOME_LON), (TEST_WORKSITE_LAT, TEST_WORKSITE_LON), (0, 0)]:
            result = r.resolve(lat, lon)
            assert isinstance(result["zone_id"], str)
            assert isinstance(result["confidence"], float)

    def test_daily_summary_without_network(self, tmp_path: Path):
        """daily_summary requires no network — pure computation + local file I/O."""
        config = {**TEST_CONFIG, "data_dir": str(tmp_path)}
        r = LocationZoneResolver(config=config)
        samples = [
            {"ts": "2026-05-22T06:00:00+00:00", "lat": TEST_HOME_LAT, "lon": TEST_HOME_LON, "source": "test"},
            {"ts": "2026-05-22T20:00:00+00:00", "lat": TEST_HOME_LAT, "lon": TEST_HOME_LON, "source": "test"},
        ]
        result = r.daily_summary(samples, "2026-05-22")
        assert result["date"] == "2026-05-22"
        assert len(result["zones_visited"]) >= 1


# ── ZoneDefinition.contains() ───────────────────────────────────────────


class TestZoneDefinition:
    """Tests for ZoneDefinition boundary logic."""

    def test_contains_center(self):
        """Center of zone is contained."""
        zd = ZoneDefinition("test", "Test", 51.165, -113.96, radius_m=200)
        assert zd.contains(51.165, -113.96)

    def test_outside_zone(self):
        """Point far outside the zone is not contained."""
        zd = ZoneDefinition("test", "Test", 51.165, -113.96, radius_m=200)
        assert not zd.contains(0, 0)

    def test_contains_near_edge(self):
        """A point just inside the radius is contained."""
        zd = ZoneDefinition("test", "Test", 51.165, -113.96, radius_m=500)
        # ~0.002 deg ≈ ~200-300m — should be inside 500m radius
        assert zd.contains(51.1665, -113.963)