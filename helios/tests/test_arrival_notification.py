"""Helios v6 — Arrival notification tests.

Tests for:
  - Location module surfacing zone_transition in tick() output
  - Engine _dispatch_arrival_notification() logic
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

# ── Location module: zone_transition in output ──────────────────────────────

from helios.modules.location import LocationModule


# Helper: the state file path used by _dispatch_arrival_notification
# The method uses: Path.home() / ".hermes" / "helios" / "data" / "intelligence_state.json"
STATE_FILE_NAME = "intelligence_state.json"


class TestLocationZoneTransitionOutput:
    """Verify that the Location module surfaces zone_transition keys in its tick() output."""

    @patch.object(LocationModule, "_poll_ha")
    def test_arrival_transition_in_output(self, mock_poll_ha, tmp_path):
        """When a zone transition occurs (away→home), the output should contain
        zone_transition='arrival', from_zone, to_zone, and zone_state keys."""
        mock_poll_ha.return_value = {
            "lat": 40.7128,
            "lon": -74.0060,
            "accuracy": 10,
            "source": "home_assistant",
            "name": "iPhone (gps)",
            "zone": "home",
        }

        data_dir = tmp_path / "helios" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        with patch("helios.modules.location.DATA_DIR", data_dir), \
             patch("helios.modules.location.LOCATION_FILE", data_dir / "icloud_location_sync.json"), \
             patch("helios.modules.location.LOCATION_HISTORY", data_dir / "location_history.jsonl"), \
             patch("helios.modules.location.LocationModule._geocode", return_value={"city": "Anytown", "province": "State"}), \
             patch.dict(os.environ, {"HASS_TOKEN": "test-token"}):
            mod = LocationModule(db_path=str(tmp_path / "test.db"), config={})
            mod._ha_last_poll = 0.0
            mod._ha_token = "test-token"
            mod._last_zone = "away"
            mod._zone_resolver._zones = [
                {"label": "home", "lat": 40.7128, "lon": -74.0060, "radius_m": 200}
            ]
            result = mod.tick()

        assert "zone_transition" in result
        assert "zone_state" in result
        assert result["zone_transition"] == "arrival"
        assert result["from_zone"] == "away"
        assert result["to_zone"] == "home"
        # Privacy: raw coords must NOT leak
        assert "lat" not in result
        assert "lon" not in result

    @patch.object(LocationModule, "_poll_ha")
    def test_no_transition_default_empty(self, mock_poll_ha, tmp_path):
        """When no zone transition occurs, zone_transition should be empty string."""
        mock_poll_ha.return_value = {
            "lat": 40.7128,
            "lon": -74.0060,
            "accuracy": 10,
            "source": "home_assistant",
            "name": "iPhone (gps)",
            "zone": "home",
        }

        data_dir = tmp_path / "helios" / "data2"
        data_dir.mkdir(parents=True, exist_ok=True)

        with patch("helios.modules.location.DATA_DIR", data_dir), \
             patch("helios.modules.location.LOCATION_FILE", data_dir / "icloud_location_sync.json"), \
             patch("helios.modules.location.LOCATION_HISTORY", data_dir / "location_history.jsonl"), \
             patch("helios.modules.location.LocationModule._geocode", return_value={"city": "Anytown", "province": "State"}), \
             patch.dict(os.environ, {"HASS_TOKEN": "test-token"}):
            mod = LocationModule(db_path=str(tmp_path / "test2.db"), config={})
            mod._ha_last_poll = 0.0
            mod._ha_token = "test-token"
            mod._last_zone = "home"
            mod._zone_resolver._zones = [
                {"label": "home", "lat": 40.7128, "lon": -74.0060, "radius_m": 200}
            ]
            result = mod.tick()

        assert result.get("zone_transition", "") == ""
        assert result.get("zone_state", "") == "home"

    @patch.object(LocationModule, "_poll_ha")
    def test_first_tick_prev_zone_none(self, mock_poll_ha, tmp_path):
        """First tick after daemon restart: prev_zone=None should NOT produce
        'arrival' — it should produce zone_transition='' (empty string).
        This is the restart-safety check: no false arrival notifications.
        """
        mock_poll_ha.return_value = {
            "lat": 40.7128,
            "lon": -74.0060,
            "accuracy": 10,
            "source": "home_assistant",
            "name": "iPhone (gps)",
            "zone": "home",
        }

        data_dir = tmp_path / "helios" / "data_restart"
        data_dir.mkdir(parents=True, exist_ok=True)

        with patch("helios.modules.location.DATA_DIR", data_dir), \
             patch("helios.modules.location.LOCATION_FILE", data_dir / "icloud_location_sync.json"), \
             patch("helios.modules.location.LOCATION_HISTORY", data_dir / "location_history.jsonl"), \
             patch("helios.modules.location.LocationModule._geocode", return_value={"city": "Anytown", "province": "State"}), \
             patch.dict(os.environ, {"HASS_TOKEN": "test-token"}):
            mod = LocationModule(db_path=str(tmp_path / "test_restart.db"), config={})
            mod._ha_last_poll = 0.0
            mod._ha_token = "test-token"
            # _last_zone is None by default (fresh module = daemon restart)
            assert mod._last_zone is None
            mod._zone_resolver._zones = [
                {"label": "home", "lat": 40.7128, "lon": -74.0060, "radius_m": 200}
            ]
            result = mod.tick()

        # Key assertion: transition must be empty string, not None
        assert result.get("zone_transition") == "", \
            f"Expected zone_transition='' on first tick, got {result.get('zone_transition')!r}"
        assert result.get("from_zone") == "", \
            f"Expected from_zone='' on first tick, got {result.get('from_zone')!r}"
        assert result.get("to_zone") == "", \
            f"Expected to_zone='' on first tick, got {result.get('to_zone')!r}"
        # zone_state should reflect current zone (home)
        assert result.get("zone_state") == "home"

    @patch.object(LocationModule, "_poll_ha")
    def test_departure_transition(self, mock_poll_ha, tmp_path):
        """When a departure transition occurs (home→away), zone_transition='departure'."""
        mock_poll_ha.return_value = {
            "lat": 51.0,
            "lon": -114.0,
            "accuracy": 50,
            "source": "home_assistant",
            "name": "iPhone (gps)",
            "zone": "away",
        }

        data_dir = tmp_path / "helios" / "data3"
        data_dir.mkdir(parents=True, exist_ok=True)

        with patch("helios.modules.location.DATA_DIR", data_dir), \
             patch("helios.modules.location.LOCATION_FILE", data_dir / "icloud_location_sync.json"), \
             patch("helios.modules.location.LOCATION_HISTORY", data_dir / "location_history.jsonl"), \
             patch("helios.modules.location.LocationModule._geocode", return_value={"city": "Anytown", "province": "State"}), \
             patch.dict(os.environ, {"HASS_TOKEN": "test-token"}):
            mod = LocationModule(db_path=str(tmp_path / "test3.db"), config={})
            mod._ha_last_poll = 0.0
            mod._ha_token = "test-token"
            mod._last_zone = "home"
            mod._zone_resolver._zones = [
                {"label": "home", "lat": 40.7128, "lon": -74.0060, "radius_m": 200}
            ]
            result = mod.tick()

        assert result.get("zone_transition") == "departure"
        assert result.get("from_zone") == "home"
        assert result.get("to_zone") == "away"


# ── Engine arrival notification dispatch ─────────────────────────────────────

from helios.engine import HeliosEngine
from helios.channels.base import ChannelResult


def _create_mock_engine():
    """Create a mock engine that has just enough to test _dispatch_arrival_notification.

    This avoids the heavy initialization of the full HeliosEngine by creating a
    mock object and binding the real method to it.
    """
    engine = MagicMock(spec=HeliosEngine)
    engine._dispatch_arrival_notification = HeliosEngine._dispatch_arrival_notification.__get__(engine)
    engine.health = MagicMock()
    engine.health.summary.return_value = {}
    return engine


def _setup_state_file(tmp_path: Path, content: dict = None) -> Path:
    """Create state file at the correct path: tmp/.hermes/helios/data/intelligence_state.json"""
    if content is None:
        content = {}
    state_file = tmp_path / ".hermes" / "helios" / "data" / "intelligence_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(content))
    return state_file


class TestArrivalNotificationDispatch:
    """Test engine._dispatch_arrival_notification() logic directly.

    We use a mock engine with the real method bound to it, avoiding heavy
    HeliosEngine initialization.
    """

    def test_arrival_triggers_notification(self, tmp_path):
        """zone_transition='arrival' within time window should dispatch notification."""
        engine = _create_mock_engine()
        mdt = ZoneInfo("America/Edmonton")
        fake_now_mdt = datetime(2026, 6, 3, 17, 30, 0, tzinfo=mdt)

        _setup_state_file(tmp_path, {})

        context = {
            "location": {
                "zone_transition": "arrival",
                "from_zone": "away",
                "to_zone": "home",
                "zone_state": "home",
            },
            "health": {"sleep_hours": 7.5, "steps": 8500},
            "spotify": {"minutes_today": 120},
        }

        with patch("helios.engine.datetime") as mock_dt, \
             patch("helios.engine.Path.home", return_value=tmp_path):
            mock_dt.now.return_value = fake_now_mdt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = engine._dispatch_arrival_notification(context)

        assert "sent" in result
        engine._emit_status.assert_called_once()
        call_kwargs = engine._emit_status.call_args.kwargs
        assert call_kwargs.get("category") == "arrival"
        assert call_kwargs.get("priority") == 1

    def test_departure_does_not_trigger(self):
        """zone_transition='departure' should NOT trigger arrival notification."""
        engine = _create_mock_engine()
        context = {
            "location": {
                "zone_transition": "departure",
                "from_zone": "home",
                "to_zone": "away",
            },
            "health": {},
            "spotify": {},
        }

        result = engine._dispatch_arrival_notification(context)

        assert result == "no_arrival"
        engine._emit_status.assert_not_called()

    def test_empty_transition_does_not_trigger(self):
        """Empty zone_transition should NOT trigger arrival notification."""
        engine = _create_mock_engine()
        context = {
            "location": {"zone_transition": "", "zone_state": "home"},
            "health": {},
            "spotify": {},
        }

        result = engine._dispatch_arrival_notification(context)

        assert result == "no_arrival"
        engine._emit_status.assert_not_called()

    def test_time_window_rejection_early(self):
        """Arrival at 10 AM MDT (hour=10) should be rejected."""
        engine = _create_mock_engine()
        mdt = ZoneInfo("America/Edmonton")
        fake_now_mdt = datetime(2026, 6, 3, 10, 0, 0, tzinfo=mdt)

        with patch("helios.engine.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now_mdt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            context = {
                "location": {"zone_transition": "arrival", "from_zone": "away", "to_zone": "home"},
                "health": {},
                "spotify": {},
            }

            result = engine._dispatch_arrival_notification(context)

        assert "outside_window" in result
        engine._emit_status.assert_not_called()

    def test_time_window_rejection_late_night(self):
        """Arrival at 23:30 MDT (hour=23) should be rejected (window is 15-22 inclusive)."""
        engine = _create_mock_engine()
        mdt = ZoneInfo("America/Edmonton")
        fake_now_mdt = datetime(2026, 6, 3, 23, 30, 0, tzinfo=mdt)

        with patch("helios.engine.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now_mdt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            context = {
                "location": {"zone_transition": "arrival", "from_zone": "away", "to_zone": "home"},
                "health": {},
                "spotify": {},
            }

            result = engine._dispatch_arrival_notification(context)

        assert "outside_window" in result
        engine._emit_status.assert_not_called()

    def test_time_window_edge_15_ok(self, tmp_path):
        """Arrival at exactly 15:00 MDT (hour=15) should be accepted."""
        engine = _create_mock_engine()
        mdt = ZoneInfo("America/Edmonton")
        fake_now_mdt = datetime(2026, 6, 3, 15, 0, 0, tzinfo=mdt)

        _setup_state_file(tmp_path, {})

        with patch("helios.engine.datetime") as mock_dt, \
             patch("helios.engine.Path.home", return_value=tmp_path):
            mock_dt.now.return_value = fake_now_mdt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            context = {
                "location": {"zone_transition": "arrival", "from_zone": "away", "to_zone": "home"},
                "health": {},
                "spotify": {},
            }

            result = engine._dispatch_arrival_notification(context)

        assert "sent" in result

    def test_time_window_edge_22_ok(self, tmp_path):
        """Arrival at exactly 22:59 MDT (hour=22) should be accepted."""
        engine = _create_mock_engine()
        mdt = ZoneInfo("America/Edmonton")
        fake_now_mdt = datetime(2026, 6, 3, 22, 59, 0, tzinfo=mdt)

        _setup_state_file(tmp_path, {})

        with patch("helios.engine.datetime") as mock_dt, \
             patch("helios.engine.Path.home", return_value=tmp_path):
            mock_dt.now.return_value = fake_now_mdt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            context = {
                "location": {"zone_transition": "arrival", "from_zone": "away", "to_zone": "home"},
                "health": {},
                "spotify": {},
            }

            result = engine._dispatch_arrival_notification(context)

        assert "sent" in result

    def test_dedup_prevents_double_notification(self, tmp_path):
        """Once-per-day dedup: second call should return already_sent."""
        engine = _create_mock_engine()
        mdt = ZoneInfo("America/Edmonton")
        fake_now_mdt = datetime(2026, 6, 3, 17, 30, 0, tzinfo=mdt)
        today_str = "2026-06-03"

        # Pre-populate state file with the dedup key already set
        _setup_state_file(tmp_path, {f"arrival_{today_str}": True})

        with patch("helios.engine.datetime") as mock_dt, \
             patch("helios.engine.Path.home", return_value=tmp_path):
            mock_dt.now.return_value = fake_now_mdt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            context = {
                "location": {"zone_transition": "arrival", "from_zone": "away", "to_zone": "home"},
                "health": {"sleep_hours": 7.0, "steps": 5000},
                "spotify": {"minutes_today": 45},
            }

            result = engine._dispatch_arrival_notification(context)

        assert "already_sent" in result
        engine._emit_status.assert_not_called()

    def test_arrival_with_health_context_in_message(self, tmp_path):
        """Verify health context (sleep, steps, spotify) appears in notification message."""
        engine = _create_mock_engine()
        mdt = ZoneInfo("America/Edmonton")
        fake_now_mdt = datetime(2026, 6, 3, 17, 30, 0, tzinfo=mdt)

        _setup_state_file(tmp_path, {})

        with patch("helios.engine.datetime") as mock_dt, \
             patch("helios.engine.Path.home", return_value=tmp_path):
            mock_dt.now.return_value = fake_now_mdt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            context = {
                "location": {
                    "zone_transition": "arrival",
                    "from_zone": "away",
                    "to_zone": "home",
                },
                "health": {"sleep_hours": 7.5, "steps": 8500},
                "spotify": {"minutes_today": 120},
            }

            result = engine._dispatch_arrival_notification(context)

        assert "sent" in result
        call_kwargs = engine._emit_status.call_args.kwargs
        message = call_kwargs.get("message", "")
        assert "7.5h" in message
        assert "8500" in message
        assert "120" in message

    def test_missing_location_context_no_crash(self):
        """Missing location key in context should not crash — treat as no arrival."""
        engine = _create_mock_engine()
        context = {}  # No location key at all

        result = engine._dispatch_arrival_notification(context)

        assert result == "no_arrival"
        engine._emit_status.assert_not_called()

    def test_dedup_state_written_on_send(self, tmp_path):
        """Verify the dedup key is written to intelligence_state.json on successful send."""
        engine = _create_mock_engine()
        mdt = ZoneInfo("America/Edmonton")
        fake_now_mdt = datetime(2026, 6, 3, 17, 30, 0, tzinfo=mdt)

        state_file = _setup_state_file(tmp_path, {})

        with patch("helios.engine.datetime") as mock_dt, \
             patch("helios.engine.Path.home", return_value=tmp_path):
            mock_dt.now.return_value = fake_now_mdt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            context = {
                "location": {
                    "zone_transition": "arrival",
                    "from_zone": "away",
                    "to_zone": "home",
                },
                "health": {},
                "spotify": {},
            }

            result = engine._dispatch_arrival_notification(context)

        assert "sent" in result
        updated_state = json.loads(state_file.read_text())
        assert "arrival_2026-06-03" in updated_state
        assert updated_state["arrival_2026-06-03"] is True

    def test_arrival_message_format(self, tmp_path):
        """Verify arrival message contains key elements: time, zones, context."""
        engine = _create_mock_engine()
        mdt = ZoneInfo("America/Edmonton")
        fake_now_mdt = datetime(2026, 6, 3, 18, 45, 0, tzinfo=mdt)

        _setup_state_file(tmp_path, {})

        with patch("helios.engine.datetime") as mock_dt, \
             patch("helios.engine.Path.home", return_value=tmp_path):
            mock_dt.now.return_value = fake_now_mdt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            context = {
                "location": {
                    "zone_transition": "arrival",
                    "from_zone": "away",
                    "to_zone": "home",
                },
                "health": {"sleep_hours": 6.5, "steps": 3200},
                "spotify": {"minutes_today": 45},
            }

            result = engine._dispatch_arrival_notification(context)

        call_kwargs = engine._emit_status.call_args.kwargs
        message = call_kwargs.get("message", "")
        # Check message contains arrival marker, transition info, and context
        assert "Arrived home" in message
        assert "away" in message
        assert "home" in message
        assert "Sleep" in message
        assert "Steps" in message
        assert "Spotify" in message