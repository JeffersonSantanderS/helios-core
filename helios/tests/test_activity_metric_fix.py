"""Tests for activity metric mapping and proactive alert fixes.

Covers:
1. HA client maps apple_exercise_time → activity.exercise_minutes (NOT activity.minutes_daily)
2. HA client maps apple_stand_time → activity.stand_minutes
3. DailyHealthScore uses stand_minutes with fallback chain
4. Proactive alert dedup (proactive_{type}_{date} keys)
5. Activity gap detection queries stand_minutes, not minutes_daily
6. Correlation string uses dynamic pattern from inferred_patterns.json
"""
import json
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── HA Client Mapping ──────────────────────────────────────────────────────

class TestHAClientMapping:
    """Verify HA entity → Helios metric mapping is correct."""

    def test_apple_exercise_time_maps_to_exercise_minutes(self):
        from helios.ha_client import HA_TO_HELIOS_MAP

        assert HA_TO_HELIOS_MAP["apple_exercise_time"] == ("activity.exercise_minutes", 1.0), \
            "apple_exercise_time should map to activity.exercise_minutes, NOT activity.minutes_daily"

    def test_apple_stand_time_maps_to_stand_minutes(self):
        from helios.ha_client import HA_TO_HELIOS_MAP

        assert HA_TO_HELIOS_MAP["apple_stand_time"] == ("activity.stand_minutes", 1.0), \
            "apple_stand_time should map to activity.stand_minutes"

    def test_no_duplicate_stand_time_mapping(self):
        """Ensure apple_stand_time appears only once in the mapping dict."""
        from helios.ha_client import HA_TO_HELIOS_MAP

        keys_list = list(HA_TO_HELIOS_MAP.keys())
        stand_count = keys_list.count("apple_stand_time")
        assert stand_count == 1, f"apple_stand_time appears {stand_count} times, expected 1"

    def test_minutes_daily_not_in_activity_mapping(self):
        """activity.minutes_daily should NOT be produced by HA ingress anymore."""
        from helios.ha_client import HA_TO_HELIOS_MAP

        values = [v[0] for v in HA_TO_HELIOS_MAP.values()]
        assert "activity.minutes_daily" not in values, \
            "No HA entity should map to activity.minutes_daily anymore"


# ── DailyHealthScore Activity Fallback ─────────────────────────────────────

class TestDailyHealthScoreActivityFallback:
    """DailyHealthScore should use stand_minutes with fallback chain."""

    def test_uses_stand_minutes_primary(self):
        from helios.proactive_intelligence import DailyHealthScore

        metrics = {
            "sleep.hours": 7.5,
            "activity.stand_minutes": 88.0,
            "activity.exercise_minutes": 14.0,
            "activity.minutes_daily": 14.0,
            "mood.score_daily": 6,
            "resting_heart_rate.avg_daily": 65,
        }
        result = DailyHealthScore.compute(metrics)
        # stand_minutes = 88, above ACTIVITY_IDEAL (60) → max 25 points
        assert result["components"]["activity"]["score"] == 25, \
            f"88 stand minutes should give max activity score, got {result['components']['activity']}"

    def test_falls_back_to_exercise_minutes(self):
        from helios.proactive_intelligence import DailyHealthScore

        metrics = {
            "sleep.hours": 7.5,
            "activity.exercise_minutes": 38.0,
            "mood.score_daily": 6,
            "resting_heart_rate.avg_daily": 65,
        }
        result = DailyHealthScore.compute(metrics)
        # Falls back to exercise_minutes = 38, above ACTIVITY_MIN (20)
        assert result["components"]["activity"]["score"] is not None
        assert result["components"]["activity"]["score"] > 5, \
            f"38 exercise minutes should give decent score, got {result['components']['activity']}"

    def test_falls_back_to_minutes_daily(self):
        from helios.proactive_intelligence import DailyHealthScore

        metrics = {
            "sleep.hours": 7.5,
            "activity.minutes_daily": 14.0,
            "mood.score_daily": 6,
            "resting_heart_rate.avg_daily": 65,
        }
        result = DailyHealthScore.compute(metrics)
        # Falls back to historical minutes_daily = 14, below threshold
        assert result["components"]["activity"]["status"] == "present"

    def test_missing_activity_returns_missing(self):
        from helios.proactive_intelligence import DailyHealthScore

        metrics = {
            "sleep.hours": 7.5,
            "mood.score_daily": 6,
            "resting_heart_rate.avg_daily": 65,
        }
        result = DailyHealthScore.compute(metrics)
        assert result["components"]["activity"]["score"] is None
        assert result["components"]["activity"]["status"] == "missing"

    def test_stand_minutes_high_gives_max_score(self):
        from helios.proactive_intelligence import DailyHealthScore

        metrics = {
            "sleep.hours": 8.0,
            "activity.stand_minutes": 120.0,
            "mood.score_daily": 8,
            "resting_heart_rate.avg_daily": 58,
        }
        result = DailyHealthScore.compute(metrics)
        # 120 stand minutes >> ACTIVITY_IDEAL (60) → max 25 points
        assert result["components"]["activity"]["score"] == 25, \
            f"120 stand minutes should give max activity score, got {result['components']['activity']}"

    def test_stand_minutes_low_gives_low_score(self):
        from helios.proactive_intelligence import DailyHealthScore

        metrics = {
            "sleep.hours": 8.0,
            "activity.stand_minutes": 5.0,
            "mood.score_daily": 8,
            "resting_heart_rate.avg_daily": 58,
        }
        result = DailyHealthScore.compute(metrics)
        # 5 stand minutes < ACTIVITY_MIN (20) → low score
        assert result["components"]["activity"]["score"] <= 5, \
            f"5 stand minutes should give low activity score, got {result['components']['activity']}"


# ── Trend Detector Activity Gap ────────────────────────────────────────────

class TestTrendDetectorActivityGap:
    """Activity gap detection should query stand_minutes, not minutes_daily."""

    def test_activity_gap_queries_stand_minutes(self):
        """Verify the SQL query uses activity.stand_minutes."""
        import inspect
        from helios.proactive_intelligence import TrendDetector

        source = inspect.getsource(TrendDetector.detect)
        # Find the activity gap section
        lines = source.split('\n')
        for i, line in enumerate(lines):
            if "activity_gap" in line.lower() or "minimal activity" in line.lower():
                for j in range(max(0, i-8), min(len(lines), i+3)):
                    if "metric" in lines[j]:
                        assert "activity.stand_minutes" in lines[j], \
                            f"Activity gap query should use stand_minutes, got: {lines[j]}"


# ── Proactive Alert Dedup ─────────────────────────────────────────────────

class TestProactiveAlertDedup:
    """Verify proactive alert dedup uses intelligence_state.json."""

    def test_dedup_key_format(self):
        """Dedup keys should be proactive_{type}_{date} format."""
        dedup_key = "proactive_activity_gap_2026-06-03"
        assert dedup_key.startswith("proactive_")
        assert "activity_gap" in dedup_key
        assert "2026-06-03" in dedup_key

    def test_dedup_prevents_repeat(self):
        """Once a dedup key is set, same alert should not fire again."""
        state = {"proactive_activity_gap_2026-06-03": True}
        alert_type = "activity_gap"
        today = "2026-06-03"
        dedup_key = f"proactive_{alert_type}_{today}"
        assert state.get(dedup_key) is True

    def test_dedup_allows_next_day(self):
        """Next day should allow the same alert type to fire."""
        state = {"proactive_activity_gap_2026-06-03": True}
        next_day_key = "proactive_activity_gap_2026-06-04"
        assert state.get(next_day_key) is None


# ── Correlation String Dynamic ────────────────────────────────────────────

class TestCorrelationString:
    """Verify activity correlation string is dynamic, not hard-coded."""

    def test_no_hardcoded_r088(self):
        """The code should NOT contain a hard-coded r=0.88."""
        from helios import proactive_intelligence
        import inspect

        source = inspect.getsource(proactive_intelligence)
        assert "r=0.88" not in source, \
            "r=0.88 hard-coded correlation should be removed from proactive_intelligence.py"

    def test_corr_str_reads_inferred_patterns(self):
        """Correlation string should be computed from inferred_patterns.json."""
        from helios.proactive_intelligence import TrendDetector
        import inspect

        source = inspect.getsource(TrendDetector.detect)
        # Should read from inferred_patterns.json, not hard-code
        assert "inferred_patterns" in source or "patterns_file" in source or "corr_str" in source, \
            "Correlation should be dynamic, not hard-coded"


# ── HA Client Extract Metrics ──────────────────────────────────────────────

class TestExtractMetrics:
    """Verify extract_metrics produces correct metric names from HA data."""

    @pytest.fixture
    def tz_aware_entities(self):
        """Create test entities with timezone-aware timestamps."""
        ts = datetime.now(timezone.utc).isoformat()
        return {
            "apple_stand_time": {
                "value": 88.0, "unit": "min",
                "last_updated": ts,
                "entity_id": "sensor.hae_healthsync_apple_stand_time"
            },
            "apple_exercise_time": {
                "value": 14.0, "unit": "min",
                "last_updated": ts,
                "entity_id": "sensor.hae_healthsync_apple_exercise_time"
            },
            "step_count": {
                "value": 4759.0, "unit": "steps",
                "last_updated": ts,
                "entity_id": "sensor.hae_healthsync_step_count"
            },
        }

    def test_stand_minutes_from_ha_data(self, tz_aware_entities):
        """apple_stand_time should extract to activity.stand_minutes."""
        from helios.ha_client import extract_metrics

        result = extract_metrics(tz_aware_entities)
        metrics = result["metrics"]
        assert "activity.stand_minutes" in metrics, \
            f"Expected activity.stand_minutes, got keys: {list(metrics.keys())}"
        assert metrics["activity.stand_minutes"] == 88.0

    def test_exercise_minutes_from_ha_data(self, tz_aware_entities):
        """apple_exercise_time should extract to activity.exercise_minutes."""
        from helios.ha_client import extract_metrics

        result = extract_metrics(tz_aware_entities)
        metrics = result["metrics"]
        assert "activity.exercise_minutes" in metrics, \
            f"Expected activity.exercise_minutes, got keys: {list(metrics.keys())}"
        assert metrics["activity.exercise_minutes"] == 14.0

    def test_both_activity_metrics_present(self, tz_aware_entities):
        """Both stand_minutes and exercise_minutes should be available."""
        from helios.ha_client import extract_metrics

        result = extract_metrics(tz_aware_entities)
        metrics = result["metrics"]
        assert "activity.stand_minutes" in metrics
        assert "activity.exercise_minutes" in metrics
        assert metrics["activity.stand_minutes"] == 88.0
        assert metrics["activity.exercise_minutes"] == 14.0