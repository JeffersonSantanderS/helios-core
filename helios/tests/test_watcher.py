"""Tests for FileWatcher, HeliosEventHandler, and watcher integration with HeliosEngine.

Covers:
- watcher.start() called exactly once via engine.start_services()
- watcher.drain() does not cause duplicate full ticks
- stopping engine when watcher was never started is safe
- WATCH_PATHS maps file changes to correct module names
- HeliosEventHandler._map_to_modules returns correct module names
"""

from __future__ import annotations

import queue
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from helios.watcher import FileWatcher, HeliosEventHandler, WATCH_PATHS


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_minimal_config(**overrides):
    """Build a minimal config dict suitable for ConfigLoader."""
    cfg = {
        "llm": {"base_url": "http://localhost:11434", "model": "test"},
        "matrix": {"enabled": False},
        "modules": {},
        "obsidian": {"enabled": False},
        "channels": {"log": {"enabled": True}},
        "priority": {"mode": "disabled"},
        "watcher": {"enabled": True},
    }
    cfg.update(overrides)
    return cfg


def _engine_with_mocked_services():
    """Create a HeliosEngine via __new__ with minimal attrs for testing start_services."""
    from helios.engine import HeliosEngine
    engine = HeliosEngine.__new__(HeliosEngine)
    engine.watcher = None
    engine._collector_procs = []
    engine._mood_handler = None
    engine._reaction_poller = None
    return engine


# ── WATCH_PATHS mapping tests ───────────────────────────────────────────

class TestWatchPaths:
    """Verify WATCH_PATHS maps observed file changes to correct module names."""

    def test_obsidian_vault_maps_to_notes_calendar_tasks(self):
        assert "obsidian_vault" in WATCH_PATHS
        mods = WATCH_PATHS["obsidian_vault"]
        assert set(mods) == {"notes", "calendar", "tasks"}

    def test_health_data_maps_to_health(self):
        assert "health_data" in WATCH_PATHS
        assert WATCH_PATHS["health_data"] == ["health"]

    def test_mac_bridge_maps_to_focus(self):
        assert "mac_bridge.json" in WATCH_PATHS
        assert "focus" in WATCH_PATHS["mac_bridge.json"]

    def test_focus_state_maps_to_focus(self):
        assert "focus_state.json" in WATCH_PATHS
        assert "focus" in WATCH_PATHS["focus_state.json"]

    def test_spotify_state_maps_to_spotify(self):
        assert "spotify_state.json" in WATCH_PATHS
        assert "spotify" in WATCH_PATHS["spotify_state.json"]

    def test_idle_state_maps_to_focus(self):
        assert "idle_state.json" in WATCH_PATHS
        assert "focus" in WATCH_PATHS["idle_state.json"]

    def test_icloud_location_maps_to_location(self):
        assert "icloud_location_sync.json" in WATCH_PATHS
        assert "location" in WATCH_PATHS["icloud_location_sync.json"]


# ── HeliosEventHandler tests ────────────────────────────────────────────

class TestHeliosEventHandler:
    """Test _map_to_modules and cooldown behaviour."""

    def test_map_to_modules_health_path(self):
        handler = HeliosEventHandler(callback=lambda m: None, cooldown=0)
        result = handler._map_to_modules("/some/path/health_data/heart_rate.json")
        assert "health" in result

    def test_map_to_modules_obsidian_vault_path(self):
        """Path must contain the literal key substring 'obsidian_vault'"""
        handler = HeliosEventHandler(callback=lambda m: None, cooldown=0)
        result = handler._map_to_modules("/home/user/obsidian_vault/notes/daily.md")
        assert "notes" in result

    def test_map_to_modules_focus_state(self):
        handler = HeliosEventHandler(callback=lambda m: None, cooldown=0)
        result = handler._map_to_modules("/home/user/.hermes/helios/data/focus_state.json")
        assert "focus" in result

    def test_map_to_modules_unknown_path_returns_empty(self):
        handler = HeliosEventHandler(callback=lambda m: None, cooldown=0)
        result = handler._map_to_modules("/tmp/random_file.txt")
        assert result == []

    def test_map_to_modules_deduplicates(self):
        handler = HeliosEventHandler(callback=lambda m: None, cooldown=0)
        result = handler._map_to_modules("/data/focus_state.json")
        assert result.count("focus") == 1

    def test_cooldown_suppresses_repeated_triggers(self):
        triggered = []
        handler = HeliosEventHandler(
            callback=lambda m: triggered.append(m),
            cooldown=10.0,
        )
        event1 = MagicMock()
        event1.src_path = "/data/health_data/steps.json"
        event1.is_directory = False

        event2 = MagicMock()
        event2.src_path = "/data/health_data/heart.json"
        event2.is_directory = False

        handler.on_modified(event1)
        handler.on_modified(event2)
        assert triggered.count("health") == 1

    def test_cooldown_allows_after_expiry(self):
        triggered = []
        handler = HeliosEventHandler(
            callback=lambda m: triggered.append(m),
            cooldown=0.01,
        )
        event = MagicMock()
        event.src_path = "/data/health_data/steps.json"
        event.is_directory = False

        handler.on_modified(event)
        time.sleep(0.02)
        handler.on_modified(event)
        assert triggered.count("health") == 2

    def test_directory_events_ignored(self):
        triggered = []
        handler = HeliosEventHandler(callback=lambda m: triggered.append(m), cooldown=0)
        event = MagicMock()
        event.src_path = "/data/health_data"
        event.is_directory = True
        handler.on_modified(event)
        assert triggered == []


# ── FileWatcher unit tests ───────────────────────────────────────────────

class TestFileWatcher:
    """Test FileWatcher start/stop/drain behaviour."""

    def test_start_creates_observer_and_sets_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            watcher = FileWatcher(
                obsidian_vault=tmp,
                health_data_dir=tmp,
                collector_data_dir=tmp,
            )
            try:
                watcher.start()
                assert watcher._running is True
            finally:
                watcher.stop()

    def test_start_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            watcher = FileWatcher(obsidian_vault=tmp)
            try:
                watcher.start()
                assert watcher._running is True
                obs1 = watcher._observer
                watcher.start()
                assert watcher._observer is obs1
            finally:
                watcher.stop()

    def test_stop_safe_if_not_started(self):
        """Stopping a watcher that was never started should not raise."""
        watcher = FileWatcher()
        watcher.stop()

    def test_stop_sets_running_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            watcher = FileWatcher(obsidian_vault=tmp)
            watcher.start()
            assert watcher._running is True
            watcher.stop()
            assert watcher._running is False

    def test_drain_returns_queued_modules(self):
        watcher = FileWatcher()
        watcher._queue.put("health")
        watcher._queue.put("health")
        watcher._queue.put("focus")
        result = watcher.drain()
        assert sorted(result) == ["focus", "health"]

    def test_drain_empty_returns_empty(self):
        watcher = FileWatcher()
        assert watcher.drain() == []

    def test_no_valid_paths_watcher_does_not_crash_on_stop(self):
        """When no valid paths exist, start() should not start the observer
        thread, and stop() should not raise."""
        watcher = FileWatcher(
            obsidian_vault="/nonexistent/path/that/does/not/exist",
            health_data_dir="/also/nonexistent",
            collector_data_dir="/nope",
        )
        watcher.start()
        assert watcher._running is False
        watcher.stop()


# ── Engine integration tests ─────────────────────────────────────────────

class TestWatcherEngineIntegration:
    """Test that HeliosEngine creates and starts the watcher correctly.

    Uses __new__ + direct attribute setup to avoid the heavy __init__.
    """

    def test_watcher_start_called_once(self):
        """When start_services() is called with watcher enabled, watcher.start()
        should be called exactly once."""
        engine = _engine_with_mocked_services()
        engine.cfg = MagicMock()
        engine.cfg._data = {
            "watcher": {"enabled": True},
            "obsidian": {"vault_path": "/tmp/test_vault"},
        }

        mock_watcher = MagicMock()
        # FileWatcher is imported lazily from .watcher in start_services(),
        # so we patch it in the watcher module itself
        with patch("helios.watcher.FileWatcher", return_value=mock_watcher), \
             patch("helios.mood_handler.MoodHandler") as MockMH, \
             patch("helios.reaction_poller.ReactionPoller") as MockRP, \
             patch.object(engine, "_start_collectors"):
            engine.start_services()

        mock_watcher.start.assert_called_once()
        assert engine.watcher is mock_watcher

    def test_watcher_drain_no_duplicate_ticks(self):
        """When watcher.drain() returns module names, tick() calls
        tick_targeted once with those names — no duplicate full ticks."""
        from helios.engine import HeliosEngine

        engine = HeliosEngine.__new__(HeliosEngine)
        engine.watcher = MagicMock()
        engine.watcher.drain.return_value = ["health", "focus"]
        engine.modules = []
        engine.cb = MagicMock()
        engine.db = MagicMock()
        engine.db.db_path = ":memory:"
        engine.db._conn = MagicMock(return_value=None)
        engine.health = MagicMock()
        engine.preferences = MagicMock()
        engine.preferences.is_quiet_hours.return_value = True
        engine.preferences.get.return_value = 7
        engine.preferences.maybe_refresh_patterns = MagicMock()
        engine.rules_engine = MagicMock()
        engine.rules_engine.evaluate.return_value = []
        engine.cfg = MagicMock()
        engine.cfg._data = {"priority": {"mode": "disabled"}}
        engine._dream_notified = set()
        engine._consecutive_tick_failures = 0

        # Track tick_targeted calls
        tick_targeted_calls = []
        def track_tick_targeted(module_names):
            tick_targeted_calls.append(list(module_names))
            return {}
        engine.tick_targeted = track_tick_targeted

        # Stub out all heavy tick side-effects
        engine._maybe_send_mood_checkin = MagicMock()
        engine.tick_reactions = MagicMock()
        engine._dispatch_module_notifications = MagicMock()
        engine._run_focus_retention = MagicMock()
        engine._maybe_push_weekly = MagicMock()
        engine.dream_engine = MagicMock()
        engine.dream_engine.should_dream.return_value = False
        engine.predictor = MagicMock()
        engine.predictor.tick_check.return_value = []
        engine.healer = MagicMock()
        engine.healer.tick_check.return_value = []
        engine.correlator = MagicMock()
        engine.outcomes = MagicMock()
        engine.outcomes.evaluate_predictions = MagicMock()
        engine.priority = MagicMock()
        engine.priority_dispatcher = MagicMock()
        engine.channels = MagicMock()
        engine.timeline = MagicMock()
        engine.timeline.normalize.return_value = 0
        engine.compressor = MagicMock()
        engine.compressor.compress.return_value = 0
        engine.obsidian = MagicMock()
        engine.obsidian.enabled = False
        engine._daily_scan_done_today = ""
        engine._priority_summary_sent_today = ""
        engine._last_insight_ts = 0.0
        engine._last_retention_ts = 0.0

        with patch("helios.engine.run_ingestion", return_value={}), \
             patch("helios.engine.narrative_templates"), \
             patch("helios.engine.generate_all_insights"), \
             patch("helios.engine.write_all_exports"), \
             patch("helios.engine.daily_intelligence"), \
             patch("helios.engine.json"):
            # These attributes exist on real engine; set them directly
            engine.llm_bridge = MagicMock()
            engine.llm_bridge.process_pending.return_value = []
            engine.dm_listener = MagicMock()
            engine.dm_listener.poll_once.return_value = []
            engine._reaction_poller = None

            engine.priority.evaluate_tick.side_effect = Exception("disabled")
            engine.priority.get_suppressed_rule_slugs.return_value = set()

            result = engine.tick()

        assert tick_targeted_calls == [["health", "focus"]]

    def test_watcher_stop_safe_if_not_started(self):
        """Stopping engine when watcher was never started should not error."""
        engine = _engine_with_mocked_services()
        assert engine.watcher is None

        # Need to also mock db since close() calls self.db.close()
        engine.db = MagicMock()
        engine.close()  # should not raise

    def test_watcher_created_with_config_paths(self):
        """FileWatcher should be created with obsidian_vault from config."""
        engine = _engine_with_mocked_services()
        engine.cfg = MagicMock()
        engine.cfg._data = {
            "watcher": {"enabled": True, "cooldown_secs": 45},
            "obsidian": {"vault_path": "/tmp/test_vault"},
        }

        mock_watcher = MagicMock()
        with patch("helios.watcher.FileWatcher") as MockWatcher, \
             patch("helios.mood_handler.MoodHandler"), \
             patch("helios.reaction_poller.ReactionPoller"), \
             patch.object(engine, "_start_collectors"):
            MockWatcher.return_value = mock_watcher
            engine.start_services()

            call_kwargs = MockWatcher.call_args[1]
            assert call_kwargs["obsidian_vault"] == "/tmp/test_vault"
            assert "health_data_dir" in call_kwargs
            assert "collector_data_dir" in call_kwargs
            assert call_kwargs["cooldown"] == 45

            mock_watcher.start.assert_called_once()

    def test_watcher_disabled_in_config(self):
        """If watcher.enabled is False in config, start_services should set
        watcher=None and never create a FileWatcher."""
        engine = _engine_with_mocked_services()
        engine.cfg = MagicMock()
        engine.cfg._data = {
            "watcher": {"enabled": False},
            "obsidian": {"vault_path": "/tmp/test_vault"},
        }

        with patch("helios.watcher.FileWatcher") as MockWatcher, \
             patch("helios.mood_handler.MoodHandler"), \
             patch("helios.reaction_poller.ReactionPoller"), \
             patch.object(engine, "_start_collectors"):
            engine.start_services()

            MockWatcher.assert_not_called()
            assert engine.watcher is None

    def test_close_calls_watcher_stop(self):
        """close() should call watcher.stop() when watcher exists."""
        from helios.engine import HeliosEngine

        engine = HeliosEngine.__new__(HeliosEngine)
        mock_watcher = MagicMock()
        engine.watcher = mock_watcher
        engine._collector_procs = []
        engine._mood_handler = None
        engine.db = MagicMock()

        engine.close()

        mock_watcher.stop.assert_called_once()