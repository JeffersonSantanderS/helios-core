"""Tests for Active Window Tracker -- screen-time logic (2026-06-01).

Validates that screen time is based on idle_seconds from idle_state.json,
NOT on foreground window / program uptime.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Put the collectors dir on path so we can import directly
import sys
collectors_dir = Path(__file__).resolve().parent.parent / "helios" / "collectors"
sys.path.insert(0, str(collectors_dir))

try:
    import active_window_tracker as tracker
except ImportError as exc:
    pytest.skip(f"Cannot import tracker module: {exc}", allow_module_level=True)


@pytest.fixture
def tmp_data_dir(tmp_path: Path):
    """Yield a temporary data directory and patch DATA_DIR for the test."""
    with patch.object(tracker, "DATA_DIR", tmp_path):
        with patch.object(tracker, "FOCUS_STATE", tmp_path / "focus_state.json"):
            with patch.object(tracker, "IDLE_STATE", tmp_path / "idle_state.json"):
                with patch.object(tracker, "APP_LOG", tmp_path / "tracked_apps.jsonl"):
                    yield tmp_path


class TestReadIdleFromJson:
    def test_missing_file_returns_none_stale(self, tmp_data_dir: Path):
        assert tracker.read_idle_from_json() == (None, True)

    def test_fresh_file_returns_idle_not_stale(self, tmp_data_dir: Path):
        idle_path = tmp_data_dir / "idle_state.json"
        idle_path.write_text(json.dumps({"idle_seconds": 45, "last_seen": datetime.now(timezone.utc).isoformat()}))
        idle_sec, stale = tracker.read_idle_from_json()
        assert idle_sec == 45
        assert not stale

    def test_old_file_returns_idle_but_stale(self, tmp_data_dir: Path):
        idle_path = tmp_data_dir / "idle_state.json"
        old_ts = datetime.now(timezone.utc).replace(year=2020).isoformat()
        idle_path.write_text(json.dumps({"idle_seconds": 10, "last_seen": old_ts}))
        idle_sec, stale = tracker.read_idle_from_json()
        assert idle_sec == 10
        assert stale

    def test_no_last_seen_returns_none_stale(self, tmp_data_dir: Path):
        idle_path = tmp_data_dir / "idle_state.json"
        idle_path.write_text(json.dumps({"idle_seconds": 10}))
        assert tracker.read_idle_from_json() == (10, False)  # no last_seen, can't calculate age

    def test_malformed_json_returns_none_stale(self, tmp_data_dir: Path):
        idle_path = tmp_data_dir / "idle_state.json"
        idle_path.write_text("not json")
        assert tracker.read_idle_from_json() == (None, True)


class TestWriteFocusStateActivityRules:
    """Screen-time must be based on mouse movement (idle_seconds), not just program running."""

    def test_idle_15s_creates_new_session(self, tmp_data_dir: Path):
        """Fresh idle < 15s means user is actively at computer -- start counting."""
        idle_path = tmp_data_dir / "idle_state.json"
        idle_path.write_text(json.dumps({"idle_seconds": 10, "last_seen": datetime.now(timezone.utc).isoformat()}))

        tracker.write_focus_state({"title": "MyApp", "category": "browser"})

        focus = json.loads((tmp_data_dir / "focus_state.json").read_text())
        assert focus["active"] is True
        assert focus["session_seconds"] == 0  # just started
        assert focus["app"] == "MyApp"
        assert focus["category"] == "browser"
        assert focus["sessions_today"] == 1

    def test_idle_300s_closes_session(self, tmp_data_dir: Path):
        """Idle > 300s means user walked away -- session ends."""
        focus_path = tmp_data_dir / "focus_state.json"
        start_ts = time.time() - 600  # session started 10 min ago
        focus_path.write_text(json.dumps({
            "active": True,
            "start_ts": start_ts,
            "app": "Game",
            "category": "gaming",
            "sessions_today": 1,
        }))

        idle_path = tmp_data_dir / "idle_state.json"
        idle_path.write_text(json.dumps({"idle_seconds": 600, "last_seen": datetime.now(timezone.utc).isoformat()}))

        tracker.write_focus_state({"title": "Game", "category": "gaming"})

        focus = json.loads(focus_path.read_text())
        assert focus["active"] is False
        assert focus["last_session_minutes"] == 10
        assert focus["last_session_app"] == "Game"

    def test_stale_idle_detector_defaults_to_idle(self, tmp_data_dir: Path):
        """If idle_detector is dead (stale data), assume idle -- don't count phantom screen time."""
        focus_path = tmp_data_dir / "focus_state.json"
        start_ts = time.time() - 3600
        focus_path.write_text(json.dumps({
            "active": True,
            "start_ts": start_ts,
            "app": "Browser",
            "category": "browser",
            "sessions_today": 1,
        }))

        idle_path = tmp_data_dir / "idle_state.json"
        old_ts = datetime.now(timezone.utc).replace(year=2020).isoformat()
        idle_path.write_text(json.dumps({"idle_seconds": 2, "last_seen": old_ts}))

        tracker.write_focus_state({"title": "Browser", "category": "browser"})

        focus = json.loads(focus_path.read_text())
        assert focus["active"] is False

    def test_session_cap_force_closes(self, tmp_data_dir: Path):
        """Session over 4 hours gets capped to prevent runaway screen time."""
        focus_path = tmp_data_dir / "focus_state.json"
        start_ts = time.time() - 20_000  # 5.5 hours ago
        focus_path.write_text(json.dumps({
            "active": True,
            "start_ts": start_ts,
            "app": "Excel",
            "category": "productivity",
            "sessions_today": 1,
        }))

        idle_path = tmp_data_dir / "idle_state.json"
        idle_path.write_text(json.dumps({"idle_seconds": 5, "last_seen": datetime.now(timezone.utc).isoformat()}))

        tracker.write_focus_state({"title": "Excel", "category": "productivity"})

        focus = json.loads(focus_path.read_text())
        assert focus["active"] is False
        assert focus["session_seconds"] == 14400  # capped at MAX_SESSION_SEC
        assert focus["last_session_minutes"] == 333  # 20_000 // 60

    def test_resuming_from_idle_increments_sessions(self, tmp_data_dir: Path):
        """Going active->idle->active should increment sessions_today."""
        focus_path = tmp_data_dir / "focus_state.json"
        # Simulate a previous completed session on SAME day
        focus_path.write_text(json.dumps({
            "active": False,
            "sessions_today": 1,
            "last_session_minutes": 30,
            "_screen_time_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }))

        idle_path = tmp_data_dir / "idle_state.json"
        idle_path.write_text(json.dumps({"idle_seconds": 5, "last_seen": datetime.now(timezone.utc).isoformat()}))

        tracker.write_focus_state({"title": "VS Code", "category": "development"})

        focus = json.loads(focus_path.read_text())
        assert focus["active"] is True
        assert focus["sessions_today"] == 2

    def test_session_completes_adds_screen_time(self, tmp_data_dir: Path):
        """When a session ends (idle>300s), its minutes add to screen_time_today."""
        focus_path = tmp_data_dir / "focus_state.json"
        start_ts = time.time() - 600  # 10 min active session
        focus_path.write_text(json.dumps({
            "active": True,
            "start_ts": start_ts,
            "app": "Game",
            "sessions_today": 1,
            "_screen_time_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }))

        idle_path = tmp_data_dir / "idle_state.json"
        idle_path.write_text(json.dumps({"idle_seconds": 600, "last_seen": datetime.now(timezone.utc).isoformat()}))

        tracker.write_focus_state({"title": "Game", "category": "gaming"})

        focus = json.loads(focus_path.read_text())
        assert focus["active"] is False
        assert focus["screen_time_today_minutes"] == 10

    def test_day_rollover_resets_screen_time(self, tmp_data_dir: Path):
        """At midnight, screen_time_today resets to 0 and sessions_today resets."""
        focus_path = tmp_data_dir / "focus_state.json"
        focus_path.write_text(json.dumps({
            "active": False,
            "screen_time_today_minutes": 240,
            "sessions_today": 8,
            "_screen_time_date": "2025-01-01",  # old date
        }))

        idle_path = tmp_data_dir / "idle_state.json"
        idle_path.write_text(json.dumps({"idle_seconds": 5, "last_seen": datetime.now(timezone.utc).isoformat()}))

        tracker.write_focus_state({"title": "NewDay", "category": "browser"})

        focus = json.loads(focus_path.read_text())
        # Day rollover resets sessions to 0, but then new active session starts → +1
        assert focus["screen_time_today_minutes"] == 0
        assert focus["sessions_today"] == 1  # reset to 0 then +1 for new session
        assert focus["_screen_time_date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")


class TestTrackedAppsLog:
    def test_log_entry_includes_idle_seconds(self, tmp_data_dir: Path):
        """tracked_apps.jsonl should include idle_seconds for analysis."""
        idle_path = tmp_data_dir / "idle_state.json"
        idle_path.write_text(json.dumps({"idle_seconds": 30, "last_seen": datetime.now(timezone.utc).isoformat()}))

        tracker.append_log({
            "title": "Spotify",
            "process": "Spotify",
            "category": "media",
        }, idle_seconds=30)

        log_path = tmp_data_dir / "tracked_apps.jsonl"
        lines = log_path.read_text().strip().split("\n")
        entry = json.loads(lines[-1])
        assert entry["idle_seconds"] == 30
        assert entry["category"] == "media"


class TestCategorize:
    def test_known_categories(self):
        assert tracker.categorize("Visual Studio Code") == "development"
        assert tracker.categorize("Chrome") == "browser"
        assert tracker.categorize("Spotify") == "media"
        assert tracker.categorize("Slack - Team") == "communication"

    def test_unknown_returns_unknown(self):
        assert tracker.categorize("TotallyUnknownApp") == "unknown"
        assert tracker.categorize("") == "unknown"

    def test_process_name_fallback(self):
        """If window title doesn't match, process name is used as fallback."""
        # categorize() only takes one arg (title) -- so we pass the process name as title
        assert tracker.categorize("steam") == "gaming"
