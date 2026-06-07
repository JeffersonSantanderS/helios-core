"""Tests for POI/dwell detection, zone resolver, and privacy sanitizer.

Covers:
  - Dwell detection emits only after radius + duration threshold
  - Moving samples do not emit visits
  - Duplicate visits are debounced
  - Overpass failure does not break location tick
  - POI memory hit avoids network call
  - Privacy sanitizer redacts lat/lon/coordinate pairs/place lookup internals
  - Exported timeline/daily summary contains zone/place labels but no raw coordinates
  - OSM tag depth (cuisine/opening_hours) boosts confidence
  - Zone resolver reads coordinates from config only
"""
import json, time, os, tempfile, shutil, pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from helios.modules import privacy_sanitizer as ps_module
from helios.modules.privacy_sanitizer import (
    sanitize_location_event,
    sanitize_log_message,
    SENSITIVE_KEYS,
)
from helios.modules import location_poi as poi_module
from helios.modules.location_poi import POIProvider
from helios.modules.zone_resolver import LocationZoneResolver


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def isolated_poi(monkeypatch):
    """Create a POIProvider with an isolated temp memory file."""
    tmpdir = tempfile.mkdtemp()
    tmp_poi = Path(tmpdir) / "poi_memory.json"
    monkeypatch.setattr(poi_module, "POI_FILE", tmp_poi)
    provider = POIProvider(config={"poi_lookup_enabled": True})
    yield provider
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def frozen_time_dwell(monkeypatch):
    """Mock time.time() so 30 samples span 15 minutes."""
    start = 1000000.0
    call_count = [0]
    def fake_time():
        call_count[0] += 1
        return start + call_count[0] * 30  # +30s per call
    monkeypatch.setattr("time.time", fake_time)


# ── Privacy Sanitizer ────────────────────────────────────────────────

def test_sanitize_removes_raw_coords():
    event = {
        "zone": "away",
        "place_name": "Tim Hortons",
        "place_type": "cafe",
        "lat": 51.15491,
        "lon": -113.93898,
        "accuracy": 2.3,
        "source": "overpass",
        "ts": "2026-05-30T18:00:00Z",
    }
    safe = sanitize_location_event(event)
    assert "lat" not in safe
    assert "lon" not in safe
    assert "accuracy" not in safe
    assert safe["place_name"] == "Tim Hortons"
    assert safe["zone"] == "away"

def test_sanitize_downgrades_overpass_source():
    event = {"place_name": "McDonald's", "source": "overpass"}
    safe = sanitize_location_event(event)
    assert safe["source"] == "inferred"

def test_sanitize_strips_coordinate_pairs_in_strings():
    msg = "Visit at [51.1628, -113.9553] after 600s"
    clean = sanitize_log_message(msg)
    assert "51.1628" not in clean
    assert "-113.9553" not in clean
    assert "<coord-redacted>" in clean

def test_sanitize_no_poi_leaves_source_unchanged():
    event = {"zone": "home", "source": "home_assistant"}
    safe = sanitize_location_event(event)
    assert safe["source"] == "home_assistant"

def test_sanitize_log_message_preserves_key_name():
    """The backref \1 must preserve the matched key name (lat/lon/etc)."""
    msg = "lat=51.123 lon=-113.456"
    clean = sanitize_log_message(msg)
    assert clean == "lat=<redacted> lon=<redacted>"


# ── Dwell Detection ──────────────────────────────────────────────────

def test_dwell_emits_after_threshold(frozen_time_dwell):
    resolver = LocationZoneResolver()
    lat, lon = 51.15491, -113.93898
    visit = None
    for _ in range(30):
        v = resolver.update_dwell(lat, lon)
        if v is not None:
            visit = v
    assert visit is not None, "dwell never emitted within 30 samples"
    assert visit["event_type"] == "visit"
    assert visit["zone"] == "away"
    assert visit["dwell_seconds"] >= 600

def test_moving_samples_do_not_emit():
    resolver = LocationZoneResolver()
    base_lat, base_lon = 51.15491, -113.93898
    visit = None
    for i in range(30):
        visit = resolver.update_dwell(base_lat + i * 0.002, base_lon)
    assert visit is None

def test_dwell_not_emitted_before_duration(frozen_time_dwell):
    resolver = LocationZoneResolver()
    lat, lon = 51.15491, -113.93898
    for _ in range(5):
        visit = resolver.update_dwell(lat, lon)
    assert visit is None

def test_duplicate_visits_debounced(frozen_time_dwell):
    resolver = LocationZoneResolver()
    lat, lon = 51.15491, -113.93898
    # First burst — should emit
    visit1 = None
    for _ in range(30):
        v = resolver.update_dwell(lat, lon)
        if v is not None:
            visit1 = v
    assert visit1 is not None, "first burst should emit"
    # Immediate second burst — should be debounced
    visit2 = None
    for _ in range(30):
        v = resolver.update_dwell(lat, lon)
        if v is not None:
            visit2 = v
    assert visit2 is None, "second burst should be debounced"


# ── Zone Resolution ──────────────────────────────────────────────────

FAKE_HOME = {"lat": 10.0, "lon": 20.0, "radius_m": 100}
FAKE_WORK = {"lat": 30.0, "lon": 40.0, "radius_m": 200}

def test_resolve_zone_from_config():
    """Zone labels come from config, not hardcoded personal coordinates."""
    resolver = LocationZoneResolver(config={
        "zones": {"home": FAKE_HOME, "work": FAKE_WORK}
    })
    assert resolver.resolve_zone(10.0, 20.0) == "home"
    assert resolver.resolve_zone(30.0, 40.0) == "work"
    assert resolver.resolve_zone(99.0, 99.0) == "away"

def test_resolve_zone_no_config_everything_is_away():
    """With no zone config, everything classifies as away."""
    resolver = LocationZoneResolver(config={})
    assert resolver.resolve_zone(51.1642, -113.9620) == "away"
    assert resolver.resolve_zone(0.0, 0.0) == "away"

def test_zone_transition_arrival():
    resolver = LocationZoneResolver(config={
        "zones": {"home": FAKE_HOME}
    })
    event = resolver.check_zone_transition(10.0, 20.0, "away")
    assert event is not None
    assert event["event_type"] == "zone_transition"
    assert event["transition"] == "arrival"
    assert event["to_zone"] == "home"

def test_zone_transition_departure():
    resolver = LocationZoneResolver(config={
        "zones": {"home": FAKE_HOME}
    })
    event = resolver.check_zone_transition(99.0, 99.0, "home")
    assert event is not None
    assert event["event_type"] == "zone_transition"
    assert event["transition"] == "departure"

def test_zone_transition_resets_dwell():
    resolver = LocationZoneResolver()
    for _ in range(15):
        resolver.update_dwell(51.15491, -113.93898)
    resolver.check_zone_transition(51.0, -114.0, "home")
    assert len(resolver._dwell_buffer) == 0


# ── POI Provider ─────────────────────────────────────────────────────

def test_poi_memory_hit_avoids_network(isolated_poi):
    provider = isolated_poi
    provider.memory_set(51.15491, -113.93898, "Tim Hortons", "cafe")
    with patch.object(provider, "query_overpass") as mock_query:
        result = provider.lookup(51.15491, -113.93898, dwell_seconds=600)
        assert result is not None
        assert result["place_name"] == "Tim Hortons"
        assert result["source"] == "memory"
        mock_query.assert_not_called()

def test_poi_lookup_disabled_returns_none():
    provider = POIProvider(config={"poi_lookup_enabled": False})
    result = provider.lookup(51.15491, -113.93898, dwell_seconds=600)
    assert result is None

def test_poi_short_dwell_skips_lookup(isolated_poi):
    provider = isolated_poi
    provider.config["poi_min_dwell_seconds"] = 600
    with patch.object(provider, "query_overpass") as mock_query:
        result = provider.lookup(51.15491, -113.93898, dwell_seconds=30)
        assert result is None
        mock_query.assert_not_called()

def test_overpass_failure_returns_none(isolated_poi):
    provider = isolated_poi
    with patch.object(provider, "query_overpass", return_value=[]) as mock_query:
        result = provider.lookup(51.15491, -113.93898, dwell_seconds=600)
        assert result is None

def test_poi_confidence_scoring_high(isolated_poi):
    provider = isolated_poi
    with patch.object(provider, "query_overpass", return_value=[{
        "name": "Tim Hortons",
        "type": "cafe",
        "brand": "Tim Hortons",
        "lat": 51.15491,
        "lon": -113.93898,
    }]):
        result = provider.lookup(51.15491, -113.93898, dwell_seconds=600)
        assert result is not None
        assert result["confidence"] >= 0.60
        assert result["place_name"] == "Tim Hortons"

def test_poi_confidence_scoring_low_rejected(isolated_poi):
    provider = isolated_poi
    with patch.object(provider, "query_overpass", return_value=[{
        "name": "X",
        "type": "unknown",
        "brand": "",
        "lat": 51.16000,
        "lon": -113.93000,
    }]):
        result = provider.lookup(51.15491, -113.93898, dwell_seconds=600)
        assert result is None  # below threshold, not cached

def test_poi_tag_depth_boosts_confidence(isolated_poi):
    """Candidate with brand + cuisine + opening_hours scores higher than bare candidate."""
    provider = isolated_poi

    # Same base POI, minimal tags
    bare = {
        "name": "Pizza Place",
        "type": "restaurant",
        "brand": "",
        "cuisine": "",
        "opening_hours": "",
        "lat": 51.15491,
        "lon": -113.93898,
    }
    # Same POI, rich tags
    rich = {
        "name": "Pizza Place",
        "type": "restaurant",
        "brand": "Pizza Place",
        "cuisine": "italian",
        "opening_hours": "Mo-Su 11:00-23:00",
        "lat": 51.15491,
        "lon": -113.93898,
    }

    score_bare = provider._score_poi(bare, 51.15491, -113.93898)
    score_rich = provider._score_poi(rich, 51.15491, -113.93898)
    assert score_rich > score_bare, (
        f"rich tags should score higher: bare={score_bare}, rich={score_rich}"
    )
    # Rich should clear the 0.60 threshold; bare likely won't
    assert score_rich >= 0.60


# ── Export Format ────────────────────────────────────────────────────

def test_timeline_event_has_no_raw_coords():
    event = {
        "event_type": "visit",
        "zone": "away",
        "place_name": "Subway",
        "place_type": "fast_food",
        "confidence": 0.82,
        "poi_source": "overpass",
        "dwell_seconds": 620,
        "ts": 1234567890,
    }
    safe = sanitize_location_event(event)
    for key in SENSITIVE_KEYS:
        assert key not in safe, f"Sensitive key '{key}' leaked into timeline event"

def test_daily_summary_format():
    summary = {
        "date": "2026-05-30",
        "visits": [
            {"zone": "away", "place_name": "Tim Hortons", "place_type": "cafe"},
            {"zone": "away", "place_name": "Subway", "place_type": "fast_food"},
        ],
        "zone_transitions": [
            {"transition": "departure", "from_zone": "home", "to_zone": "away"},
            {"transition": "arrival", "from_zone": "away", "to_zone": "home"},
        ],
        "lat": 51.1642,
    }
    safe = sanitize_location_event(summary)
    assert "lat" not in safe
    assert safe["visits"][0]["place_name"] == "Tim Hortons"
    assert safe["zone_transitions"][0]["from_zone"] == "home"
