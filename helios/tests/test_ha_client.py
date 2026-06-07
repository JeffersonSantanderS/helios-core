"""Helios v5 — Home Assistant Client Tests.

Tests for helios.ha_client module — general HA REST client.
Uses unittest.mock to mock requests since HA may not be running.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from helios.ha_client import (
    fetch_state,
    fetch_states,
    fetch_entities_by_domain,
    fetch_entities_by_prefix,
    fetch_calendar_events,
    call_service,
    check_ha_available,
    compute_freshness,
    get_token,
)


# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

MOCK_BASE_URL = "http://homeassistant.local:8123"
MOCK_TOKEN = "test-ha-token-12345"

MOCK_ENTITY = {
    "entity_id": "device_tracker.user_iphone",
    "state": "home",
    "attributes": {
        "latitude": 40.7128,
        "longitude": -74.0060,
        "gps_accuracy": 10,
        "friendly_name": "User iPhone",
    },
    "last_changed": "2026-05-15T12:00:00+00:00",
    "last_updated": "2026-05-15T12:00:30+00:00",
}

MOCK_ALL_STATES = [
    MOCK_ENTITY,
    {
        "entity_id": "sensor.temperature",
        "state": "22.5",
        "attributes": {"unit_of_measurement": "°C"},
        "last_updated": "2026-05-15T11:59:00+00:00",
    },
    {
        "entity_id": "calendar.jefferson",
        "state": "on",
        "attributes": {},
        "last_updated": "2026-05-15T12:01:00+00:00",
    },
    {
        "entity_id": "todo.today",
        "state": "5",
        "attributes": {"friendly_name": "Today's Tasks"},
        "last_updated": "2026-05-15T12:02:00+00:00",
    },
]

MOCK_CALENDAR_EVENTS = [
    {
        "summary": "Team Standup",
        "start": {"dateTime": "2026-05-15T09:00:00+00:00"},
        "end": {"dateTime": "2026-05-15T09:30:00+00:00"},
        "location": "Zoom",
        "uid": "evt-001",
    },
    {
        "summary": "Deep Work",
        "start": {"dateTime": "2026-05-15T10:00:00+00:00"},
        "end": {"dateTime": "2026-05-15T12:00:00+00:00"},
        "location": "",
    },
]


def _mock_response(json_data=None, status_code=200, raise_error=False):
    """Create a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    if raise_error:
        resp.raise_for_status.side_effect = __import__("requests").HTTPError(
            response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ===================================================================
# 1. get_token
# ===================================================================

def test_get_token_hass_token():
    """get_token returns HASS_TOKEN if set."""
    with patch.dict(os.environ, {"HASS_TOKEN": "my-hass-token"}, clear=False):
        assert get_token() == "my-hass-token"


def test_get_token_ha_token_fallback():
    """get_token falls back to HA_TOKEN."""
    with patch.dict(os.environ, {"HA_TOKEN": "my-ha-token"}, clear=False):
        # Remove HASS_TOKEN if present
        env = dict(os.environ)
        env.pop("HASS_TOKEN", None)
        with patch.dict(os.environ, env, clear=True):
            assert get_token() == "my-ha-token"


def test_get_token_none():
    """get_token returns None when no token env vars set."""
    env = dict(os.environ)
    env.pop("HASS_TOKEN", None)
    env.pop("HA_TOKEN", None)
    with patch.dict(os.environ, env, clear=True):
        assert get_token() is None


def test_get_token_hass_preferred():
    """HASS_TOKEN takes priority over HA_TOKEN."""
    with patch.dict(os.environ, {"HASS_TOKEN": "preferred", "HA_TOKEN": "fallback"}, clear=False):
        assert get_token() == "preferred"


# ===================================================================
# 2. fetch_state
# ===================================================================

class TestFetchState:
    def test_success(self):
        """fetch_state returns entity dict on success."""
        with patch("helios.ha_client._safe_get", return_value=MOCK_ENTITY):
            result = fetch_state(MOCK_BASE_URL, MOCK_TOKEN, "device_tracker.user_iphone")
            assert result is not None
            assert result["entity_id"] == "device_tracker.user_iphone"
            assert result["state"] == "home"

    def test_failure_returns_none(self):
        """fetch_state returns None on failure."""
        with patch("helios.ha_client._safe_get", return_value=None):
            result = fetch_state(MOCK_BASE_URL, MOCK_TOKEN, "nonexistent.entity")
            assert result is None

    def test_url_format(self):
        """fetch_state uses correct URL format."""
        with patch("helios.ha_client._safe_get", return_value=MOCK_ENTITY) as mock_get:
            fetch_state(MOCK_BASE_URL, MOCK_TOKEN, "sensor.temp")
            mock_get.assert_called_once()
            url = mock_get.call_args[0][0]
            assert url == "http://homeassistant.local:8123/api/states/sensor.temp"


# ===================================================================
# 3. fetch_states
# ===================================================================

class TestFetchStates:
    def test_success(self):
        """fetch_states returns list of entities."""
        with patch("helios.ha_client._safe_get", return_value=MOCK_ALL_STATES):
            result = fetch_states(MOCK_BASE_URL, MOCK_TOKEN)
            assert len(result) == 4

    def test_failure_returns_empty(self):
        """fetch_states returns [] on failure."""
        with patch("helios.ha_client._safe_get", return_value=None):
            result = fetch_states(MOCK_BASE_URL, MOCK_TOKEN)
            assert result == []


# ===================================================================
# 4. fetch_entities_by_domain
# ===================================================================

class TestFetchEntitiesByDomain:
    def test_sensor_domain(self):
        """Fetches only sensor.* entities."""
        with patch("helios.ha_client.fetch_states", return_value=MOCK_ALL_STATES):
            result = fetch_entities_by_domain(MOCK_BASE_URL, MOCK_TOKEN, "sensor")
            assert len(result) == 1
            assert "sensor.temperature" in result

    def test_device_tracker_domain(self):
        """Fetches only device_tracker.* entities."""
        with patch("helios.ha_client.fetch_states", return_value=MOCK_ALL_STATES):
            result = fetch_entities_by_domain(MOCK_BASE_URL, MOCK_TOKEN, "device_tracker")
            assert len(result) == 1
            assert "device_tracker.user_iphone" in result

    def test_empty_domain(self):
        """Returns empty dict for domain with no entities."""
        with patch("helios.ha_client.fetch_states", return_value=MOCK_ALL_STATES):
            result = fetch_entities_by_domain(MOCK_BASE_URL, MOCK_TOKEN, "light")
            assert result == {}


# ===================================================================
# 5. fetch_entities_by_prefix
# ===================================================================

class TestFetchEntitiesByPrefix:
    def test_calendar_prefix(self):
        """Filters by calendar. prefix."""
        with patch("helios.ha_client.fetch_states", return_value=MOCK_ALL_STATES):
            result = fetch_entities_by_prefix(MOCK_BASE_URL, MOCK_TOKEN, "calendar.")
            assert len(result) == 1
            assert "calendar.jefferson" in result

    def test_hae_prefix(self):
        """Filters by hae. prefix (health auto export)."""
        states_with_hae = MOCK_ALL_STATES + [
            {
                "entity_id": "hae.healthsync_step_count",
                "state": "8500",
                "attributes": {},
                "last_updated": "2026-05-15T12:00:00+00:00",
            }
        ]
        with patch("helios.ha_client.fetch_states", return_value=states_with_hae):
            result = fetch_entities_by_prefix(MOCK_BASE_URL, MOCK_TOKEN, "hae.health")
            assert len(result) == 1
            assert "hae.healthsync_step_count" in result


# ===================================================================
# 6. fetch_calendar_events
# ===================================================================

class TestFetchCalendarEvents:
    def test_success(self):
        """fetch_calendar_events returns normalized events."""
        with patch("helios.ha_client._safe_get") as mock_get:
            mock_get.return_value = MOCK_CALENDAR_EVENTS
            result = fetch_calendar_events(
                MOCK_BASE_URL, MOCK_TOKEN,
                "calendar.jefferson",
                "2026-05-15T00:00:00+00:00",
                "2026-05-22T00:00:00+00:00",
            )
            assert len(result) == 2
            assert result[0]["title"] == "Team Standup"

    def test_failure_returns_empty(self):
        """fetch_calendar_events returns [] on failure."""
        with patch("helios.ha_client._safe_get", return_value=None):
            result = fetch_calendar_events(
                MOCK_BASE_URL, MOCK_TOKEN,
                "calendar.jefferson",
                "2026-05-15T00:00:00+00:00",
                "2026-05-22T00:00:00+00:00",
            )
            assert result == []

    def test_url_format(self):
        """Uses correct HA calendar API endpoint with query params."""
        with patch("helios.ha_client._safe_get") as mock_get:
            mock_get.return_value = []
            fetch_calendar_events(
                MOCK_BASE_URL, MOCK_TOKEN,
                "calendar.work",
                "2026-05-15T00:00:00Z",
                "2026-05-22T00:00:00Z",
            )
            url = mock_get.call_args[0][0]
            assert "/api/calendars/calendar.work" in url


# ===================================================================
# 7. call_service
# ===================================================================

class TestCallService:
    def test_success(self):
        """call_service returns response dict."""
        with patch("helios.ha_client._safe_post", return_value=[{"entity_id": "light.lamp"}]):
            result = call_service(MOCK_BASE_URL, MOCK_TOKEN, "light", "turn_on", {"entity_id": "light.lamp"})
            assert result is not None

    def test_failure_returns_none(self):
        """call_service returns None on failure."""
        with patch("helios.ha_client._safe_post", return_value=None):
            result = call_service(MOCK_BASE_URL, MOCK_TOKEN, "light", "turn_on")
            assert result is None


# ===================================================================
# 8. check_ha_available
# ===================================================================

class TestCheckHAAvailable:
    def test_available(self):
        """Returns healthy when HA is reachable."""
        with patch("helios.ha_client._safe_get", return_value={"message": "API running.", "version": "2024.1"}):
            result = check_ha_available(MOCK_BASE_URL, MOCK_TOKEN)
            assert result["available"] is True
            assert result["state"] == "healthy"
            assert result["version"] == "2024.1"

    def test_unavailable(self):
        """Returns unavailable when HA is unreachable."""
        with patch("helios.ha_client._safe_get", return_value=None):
            result = check_ha_available(MOCK_BASE_URL, MOCK_TOKEN)
            assert result["available"] is False
            assert result["state"] == "unavailable"


# ===================================================================
# 9. compute_freshness
# ===================================================================

class TestComputeFreshness:
    def test_recent_entity(self):
        """Recent entity returns small freshness value."""
        # Entity updated 30 seconds ago
        dt = (datetime.now(timezone.utc) - __import__("datetime").timedelta(seconds=30)).isoformat()
        entity = {"last_updated": dt}
        freshness = compute_freshness(entity)
        assert freshness is not None
        assert 25 <= freshness <= 35

    def test_stale_entity(self):
        """Stale entity returns large freshness value."""
        dt = (datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=24)).isoformat()
        entity = {"last_updated": dt}
        freshness = compute_freshness(entity)
        assert freshness is not None
        assert freshness > 80000

    def test_missing_last_updated(self):
        """Returns None when last_updated is missing."""
        assert compute_freshness({}) is None
        assert compute_freshness({"last_updated": None}) is None

    def test_invalid_format(self):
        """Returns None for unparseable datetime."""
        assert compute_freshness({"last_updated": "not-a-date"}) is None