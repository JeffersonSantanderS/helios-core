"""Tests for expanded correlator coverage — weather, activity, sleep architecture,
health vitals, Spotify, and nutrition correlations.

Covers:
1. All new KNOWN_PAIRS are registered
2. All new METRIC_DEFS exist with correct source_type
3. Correlator can compute weather ↔ sleep correlation with synthetic data
4. Correlator can compute steps ↔ sleep correlation
5. Correlator gracefully handles missing data for new metrics
6. No metric in KNOWN_PAIRS lacks a METRIC_DEFS entry
7. Weather-specific: temp_max negatively correlates with sleep in hot weather
"""
import json
import math
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── KNOWN_PAIRS Completeness ──────────────────────────────────────────────

class TestKnownPairsCompleteness:
    """Verify all new correlation pairs are registered."""

    def test_weather_temp_max_sleep_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("weather.temp_max", "sleep.hours") in KNOWN_PAIRS

    def test_weather_temp_max_mood_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("weather.temp_max", "mood.score") in KNOWN_PAIRS

    def test_weather_temp_max_steps_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("weather.temp_max", "activity.steps_daily") in KNOWN_PAIRS

    def test_weather_precipitation_sleep_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("weather.precipitation", "sleep.hours") in KNOWN_PAIRS

    def test_weather_precipitation_steps_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("weather.precipitation", "activity.steps_daily") in KNOWN_PAIRS

    def test_weather_temp_min_sleep_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("weather.temp_min", "sleep.hours") in KNOWN_PAIRS

    def test_steps_sleep_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("activity.steps_daily", "sleep.hours") in KNOWN_PAIRS

    def test_stand_minutes_sleep_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("activity.stand_minutes", "sleep.hours") in KNOWN_PAIRS

    def test_steps_mood_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("activity.steps_daily", "mood.score") in KNOWN_PAIRS

    def test_deep_sleep_total_sleep_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("sleep.deep_hours", "sleep.hours") in KNOWN_PAIRS

    def test_rem_mood_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("sleep.rem_hours", "mood.score") in KNOWN_PAIRS

    def test_deep_sleep_rhr_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("sleep.deep_hours", "health.resting_hr") in KNOWN_PAIRS

    def test_health_hrv_sleep_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("health.hrv_ms", "sleep.hours") in KNOWN_PAIRS

    def test_health_o2_sleep_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("health.blood_o2", "sleep.hours") in KNOWN_PAIRS

    def test_spotify_sleep_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("spotify.listen_minutes_daily", "sleep.hours") in KNOWN_PAIRS

    def test_spotify_mood_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("spotify.listen_minutes_daily", "mood.score") in KNOWN_PAIRS

    def test_calories_sleep_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("nutrition.calories_daily", "sleep.hours") in KNOWN_PAIRS

    def test_carbs_sleep_pair_exists(self):
        from helios.correlator import KNOWN_PAIRS
        assert ("nutrition.carbs_daily", "sleep.hours") in KNOWN_PAIRS

    def test_total_known_pairs_count(self):
        """Verify we have at least 25 pairs now (7 old + 18 new)."""
        from helios.correlator import KNOWN_PAIRS
        assert len(KNOWN_PAIRS) >= 25, \
            f"Expected at least 25 KNOWN_PAIRS, got {len(KNOWN_PAIRS)}"


# ── METRIC_DEFS Completeness ───────────────────────────────────────────────

class TestMetricDefsCompleteness:
    """Verify all new metric definitions exist."""

    def test_weather_temp_max_def(self):
        from helios.correlator import METRIC_DEFS
        assert "weather.temp_max" in METRIC_DEFS
        assert METRIC_DEFS["weather.temp_max"]["source_type"] == "metric_snapshots"

    def test_weather_temp_min_def(self):
        from helios.correlator import METRIC_DEFS
        assert "weather.temp_min" in METRIC_DEFS

    def test_weather_precipitation_def(self):
        from helios.correlator import METRIC_DEFS
        assert "weather.precipitation" in METRIC_DEFS

    def test_steps_daily_def(self):
        from helios.correlator import METRIC_DEFS
        assert "activity.steps_daily" in METRIC_DEFS
        assert METRIC_DEFS["activity.steps_daily"]["source_type"] == "metric_snapshots"

    def test_stand_minutes_def(self):
        from helios.correlator import METRIC_DEFS
        assert "activity.stand_minutes" in METRIC_DEFS

    def test_deep_hours_def(self):
        from helios.correlator import METRIC_DEFS
        assert "sleep.deep_hours" in METRIC_DEFS

    def test_rem_hours_def(self):
        from helios.correlator import METRIC_DEFS
        assert "sleep.rem_hours" in METRIC_DEFS

    def test_health_resting_hr_def(self):
        from helios.correlator import METRIC_DEFS
        assert "health.resting_hr" in METRIC_DEFS

    def test_health_hrv_def(self):
        from helios.correlator import METRIC_DEFS
        assert "health.hrv_ms" in METRIC_DEFS

    def test_health_blood_o2_def(self):
        from helios.correlator import METRIC_DEFS
        assert "health.blood_o2" in METRIC_DEFS

    def test_spotify_def(self):
        from helios.correlator import METRIC_DEFS
        assert "spotify.listen_minutes_daily" in METRIC_DEFS

    def test_nutrition_calories_def(self):
        from helios.correlator import METRIC_DEFS
        assert "nutrition.calories_daily" in METRIC_DEFS

    def test_nutrition_carbs_def(self):
        from helios.correlator import METRIC_DEFS
        assert "nutrition.carbs_daily" in METRIC_DEFS

    def test_total_metric_defs_count(self):
        """Verify we have at least 20 metric definitions now (7 old + 13 new)."""
        from helios.correlator import METRIC_DEFS
        assert len(METRIC_DEFS) >= 20, \
            f"Expected at least 20 METRIC_DEFS, got {len(METRIC_DEFS)}"


# ── KNOWN_PAIRS ↔ METRIC_DEFS Consistency ──────────────────────────────────

class TestPairsDefsConsistency:
    """Every metric in KNOWN_PAIRS must have a METRIC_DEFS entry."""

    def test_all_pair_metrics_have_defs(self):
        from helios.correlator import KNOWN_PAIRS, METRIC_DEFS
        missing = []
        for metric_a, metric_b in KNOWN_PAIRS:
            if metric_a not in METRIC_DEFS:
                missing.append(metric_a)
            if metric_b not in METRIC_DEFS:
                missing.append(metric_b)
        if missing:
            missing = sorted(set(missing))
            pytest.fail(f"Metrics in KNOWN_PAIRS but missing from METRIC_DEFS: {missing}")


# ── Correlator Computation with Synthetic Data ────────────────────────────

class TestCorrelatorComputation:
    """Test that the correlator can compute correlations for new metric pairs."""

    @pytest.fixture
    def temp_db(self, tmp_path):
        """Create a temporary DB with synthetic data."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE metric_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric TEXT NOT NULL,
            date_key TEXT NOT NULL,
            value REAL NOT NULL,
            source TEXT DEFAULT '',
            UNIQUE(metric, date_key, source)
        )""")
        conn.execute("""CREATE TABLE correlations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_a TEXT NOT NULL,
            metric_b TEXT NOT NULL,
            window_days INTEGER NOT NULL,
            pearson_r REAL NOT NULL,
            p_value REAL,
            strength TEXT,
            direction TEXT,
            n_observations INTEGER,
            computed_at TEXT,
            UNIQUE(metric_a, metric_b, window_days)
        )""")
        conn.execute("""CREATE TABLE mood (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            score TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE focus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            state TEXT,
            duration_secs REAL
        )""")
        conn.commit()
        return conn, db_path

    def _seed_weather_sleep_data(self, conn, n_days=14):
        """Seed synthetic data where hot days → worse sleep (negative correlation)."""
        base_date = datetime(2026, 5, 20)
        for i in range(n_days):
            date_str = (base_date + timedelta(days=i)).strftime("%Y-%m-%d")
            # Temperature varies 5-30°C
            temp_max = 5 + (i % 7) * 4.5  # cycles through 5, 9.5, 14, 18.5, 23, 27.5, 32
            # Sleep inversely correlated: hot = less sleep
            sleep_hours = 8.0 - (temp_max - 5) * 0.08  # ~8h at 5°C, ~5.6h at 32°C
            precip = 5.0 if i % 3 == 0 else 0.0
            steps = 5000 + (30 - temp_max) * 100  # more steps when cooler
            deep_sleep = sleep_hours * 0.15
            stand_mins = 60 + (30 - temp_max) * 2

            for metric, value in [
                ("weather.temp_max", temp_max),
                ("weather.temp_min", temp_max - 8),
                ("weather.precipitation", precip),
                ("sleep.hours", sleep_hours),
                ("sleep.deep_hours", deep_sleep),
                ("activity.steps_daily", steps),
                ("activity.stand_minutes", stand_mins),
                ("spotify.listen_minutes_daily", 30 + i * 5),
                ("health.hrv_ms", 40 + i),
                ("health.blood_o2", 97 + (i % 3) * 0.5),
            ]:
                conn.execute(
                    "INSERT OR REPLACE INTO metric_snapshots (metric, date_key, value, source) VALUES (?, ?, ?, 'test')",
                    (metric, date_str, value),
                )
        conn.commit()

    def test_weather_temp_max_sleep_correlation_negative(self, temp_db):
        """Hot days should correlate negatively with sleep."""
        conn, db_path = temp_db
        self._seed_weather_sleep_data(conn)

        from helios.correlator import CorrelationEngine
        correlator = CorrelationEngine(db_path=str(db_path))
        results = correlator.run_weekly_scan(
            pairs=[("weather.temp_max", "sleep.hours")],
            windows=[14],
        )
        # Find the result for this pair
        found = [r for r in results if r.get("metric_a") == "weather.temp_max" and r.get("metric_b") == "sleep.hours"]
        assert len(found) > 0, "No correlation result for weather.temp_max ↔ sleep.hours"
        r_val = found[0].get("pearson_r", 0)
        # Our synthetic data has negative correlation
        assert r_val < -0.3, f"Expected negative correlation (hot = less sleep), got r={r_val}"

    def test_steps_sleep_correlation(self, temp_db):
        """Steps and sleep should correlate positively (cooler days = more steps + more sleep)."""
        conn, db_path = temp_db
        self._seed_weather_sleep_data(conn)

        from helios.correlator import CorrelationEngine
        correlator = CorrelationEngine(db_path=str(db_path))
        results = correlator.run_weekly_scan(
            pairs=[("activity.steps_daily", "sleep.hours")],
            windows=[14],
        )
        found = [r for r in results if r.get("metric_a") == "activity.steps_daily"]
        assert len(found) > 0, "No correlation result for steps ↔ sleep"
        r_val = found[0].get("pearson_r", 0)
        # Both driven by temperature → positively correlated
        assert r_val > 0.3, f"Expected positive steps↔sleep correlation, got r={r_val}"

    def test_precipitation_sleep_correlation_computed(self, temp_db):
        """Precipitation ↔ sleep should compute without error."""
        conn, db_path = temp_db
        self._seed_weather_sleep_data(conn)

        from helios.correlator import CorrelationEngine
        correlator = CorrelationEngine(db_path=str(db_path))
        results = correlator.run_weekly_scan(
            pairs=[("weather.precipitation", "sleep.hours")],
            windows=[14],
        )
        # Should produce a result (even if weak)
        found = [r for r in results if "weather.precipitation" in (r.get("metric_a", ""), r.get("metric_b", ""))]
        assert len(found) > 0, "No correlation result for precipitation ↔ sleep"

    def test_spotify_sleep_correlation_computed(self, temp_db):
        """Spotify ↔ sleep should compute without error."""
        conn, db_path = temp_db
        self._seed_weather_sleep_data(conn)

        from helios.correlator import CorrelationEngine
        correlator = CorrelationEngine(db_path=str(db_path))
        results = correlator.run_weekly_scan(
            pairs=[("spotify.listen_minutes_daily", "sleep.hours")],
            windows=[14],
        )
        found = [r for r in results if "spotify.listen_minutes_daily" in (r.get("metric_a", ""), r.get("metric_b", ""))]
        assert len(found) > 0, "No correlation result for spotify ↔ sleep"

    def test_deep_sleep_total_sleep_correlation(self, temp_db):
        """Deep sleep should positively correlate with total sleep."""
        conn, db_path = temp_db
        self._seed_weather_sleep_data(conn)

        from helios.correlator import CorrelationEngine
        correlator = CorrelationEngine(db_path=str(db_path))
        results = correlator.run_weekly_scan(
            pairs=[("sleep.deep_hours", "sleep.hours")],
            windows=[14],
        )
        found = [r for r in results if r.get("metric_a") == "sleep.deep_hours"]
        assert len(found) > 0
        r_val = found[0].get("pearson_r", 0)
        assert r_val > 0.3, f"Deep sleep should correlate with total sleep, got r={r_val}"

    def test_missing_metric_produces_no_crash(self, temp_db):
        """If a metric pair has no data, the correlator should skip it, not crash."""
        conn, db_path = temp_db
        # Don't seed any data
        from helios.correlator import CorrelationEngine
        correlator = CorrelationEngine(db_path=str(db_path))
        # This should not raise
        results = correlator.run_weekly_scan(
            pairs=[("weather.temp_max", "sleep.hours")],
            windows=[14],
        )
        # Should return empty or zero results (no data to correlate)
        assert isinstance(results, list)

    def test_scan_all_new_pairs_no_crash(self, temp_db):
        """Running a full scan with all new pairs against synthetic data should not crash."""
        conn, db_path = temp_db
        self._seed_weather_sleep_data(conn)

        from helios.correlator import KNOWN_PAIRS, CorrelationEngine
        correlator = CorrelationEngine(db_path=str(db_path))
        # Scan all pairs — should not crash even if some have no data
        results = correlator.run_weekly_scan(pairs=KNOWN_PAIRS, windows=[7, 14])
        assert isinstance(results, list)
        # At least some should produce results
        assert len(results) > 0, f"Expected some correlation results, got 0"

    def test_stand_minutes_sleep_correlation(self, temp_db):
        """Stand minutes should correlate with sleep in synthetic data."""
        conn, db_path = temp_db
        self._seed_weather_sleep_data(conn)

        from helios.correlator import CorrelationEngine
        correlator = CorrelationEngine(db_path=str(db_path))
        results = correlator.run_weekly_scan(
            pairs=[("activity.stand_minutes", "sleep.hours")],
            windows=[14],
        )
        found = [r for r in results if r.get("metric_a") == "activity.stand_minutes"]
        assert len(found) > 0


# ── Correlation with Real Data (Integration) ──────────────────────────────

class TestCorrelatorWithRealData:
    """Test correlator against the real Helios database if available."""

    def test_weather_temp_max_sleep_real_data(self):
        """Compute weather ↔ sleep correlation from real data."""
        db_path = Path.home() / ".hermes/helios/helios_v6.db"
        if not db_path.exists():
            pytest.skip("Real DB not available")

        from helios.correlator import CorrelationEngine
        correlator = CorrelationEngine(db_path=str(db_path))
        results = correlator.run_weekly_scan(
            pairs=[("weather.temp_max", "sleep.hours")],
            windows=[14],
        )
        found = [r for r in results if r.get("metric_a") == "weather.temp_max"]
        if found:
            r_val = found[0].get("pearson_r", 0)
            n_obs = found[0].get("n_observations", 0)
            print(f"Real data: weather.temp_max ↔ sleep.hours r={r_val:.3f} n={n_obs}")
        else:
            print("No paired data available yet")

    def test_steps_sleep_real_data(self):
        """Compute steps ↔ sleep correlation from real data."""
        db_path = Path.home() / ".hermes/helios/helios_v6.db"
        if not db_path.exists():
            pytest.skip("Real DB not available")

        from helios.correlator import CorrelationEngine
        correlator = CorrelationEngine(db_path=str(db_path))
        results = correlator.run_weekly_scan(
            pairs=[("activity.steps_daily", "sleep.hours")],
            windows=[14],
        )
        found = [r for r in results if r.get("metric_a") == "activity.steps_daily"]
        if found:
            r_val = found[0].get("pearson_r", 0)
            n_obs = found[0].get("n_observations", 0)
            print(f"Real data: activity.steps_daily ↔ sleep.hours r={r_val:.3f} n={n_obs}")