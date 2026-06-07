"""Helios v5 — Cross-Module Correlation Engine Tests.

Comprehensive pytest tests for helios.correlator.CorrelationEngine.
Uses tmp_path for temporary SQLite databases, synthetic correlated data,
and verifies Pearson r calculations, p-values, storage, and rule generation.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from helios.correlator import (
    MODULE_NAME,
    KNOWN_PAIRS,
    METRIC_DEFS,
    CorrelationEngine,
    _pearson_r,
    _classify_strength,
    _classify_direction,
)

# ---------------------------------------------------------------------------
# DDL helpers — create all tables the correlator needs
# ---------------------------------------------------------------------------

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    description TEXT
);
"""

_CONTEXT_DDL = """
CREATE TABLE IF NOT EXISTS context (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    source      TEXT    NOT NULL,
    module      TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value       TEXT    NOT NULL DEFAULT '{}',
    priority    INTEGER NOT NULL DEFAULT 0,
    expires_at  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT ctx_unique_latest UNIQUE (module, key, source)
);
CREATE INDEX IF NOT EXISTS idx_context_module_ts ON context (module, ts);
CREATE INDEX IF NOT EXISTS idx_context_source_ts ON context (source, ts);
"""

_MOOD_DDL = """
CREATE TABLE IF NOT EXISTS mood (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    emoji           TEXT    NOT NULL,
    score           INTEGER NOT NULL CHECK (score BETWEEN 1 AND 10),
    note            TEXT,
    source          TEXT    NOT NULL DEFAULT 'discord_button',
    discord_msg_id  TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_mood_ts ON mood (ts);
"""

_FOCUS_DDL = """
CREATE TABLE IF NOT EXISTS focus (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    state           TEXT    NOT NULL CHECK (state IN ('working','gaming','idle','meeting','break')),
    source          TEXT    NOT NULL,
    context         TEXT    NOT NULL DEFAULT '{}',
    duration_secs   INTEGER,
    session_start   TEXT,
    session_end     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_focus_state_ts ON focus (state, ts);
"""

_CORRELATIONS_DDL = """
CREATE TABLE IF NOT EXISTS metric_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metric          TEXT    NOT NULL,
    value           REAL    NOT NULL,
    date_key        TEXT    NOT NULL,
    source          TEXT    NOT NULL DEFAULT 'correlator',
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT uq_metric_date UNIQUE (metric, date_key)
);
CREATE INDEX IF NOT EXISTS idx_metric_snapshots_metric_date ON metric_snapshots (metric, date_key);
CREATE INDEX IF NOT EXISTS idx_metric_snapshots_ts ON metric_snapshots (ts);

CREATE TABLE IF NOT EXISTS correlations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metric_a        TEXT    NOT NULL,
    metric_b        TEXT    NOT NULL,
    window_days     INTEGER NOT NULL DEFAULT 7,
    pearson_r       REAL    NOT NULL,
    p_value         REAL    NOT NULL,
    strength        TEXT    NOT NULL CHECK (strength IN ('weak', 'moderate', 'strong')),
    direction       TEXT    NOT NULL CHECK (direction IN ('positive', 'negative')),
    n_observations  INTEGER NOT NULL,
    suggested_rule  TEXT,
    approved        INTEGER NOT NULL DEFAULT 0,
    approved_by     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT uq_correlation_pair_window UNIQUE (metric_a, metric_b, window_days)
);
CREATE INDEX IF NOT EXISTS idx_correlations_strength ON correlations (strength, pearson_r DESC);
CREATE INDEX IF NOT EXISTS idx_correlations_approved ON correlations (approved);

CREATE TABLE IF NOT EXISTS correlation_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metric_a        TEXT    NOT NULL,
    metric_b        TEXT    NOT NULL,
    value_a         REAL    NOT NULL,
    value_b         REAL    NOT NULL,
    date_key        TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT uq_obs_pair_date UNIQUE (metric_a, metric_b, date_key)
);
CREATE INDEX IF NOT EXISTS idx_corr_obs_pair_date ON correlation_observations (metric_a, metric_b, date_key);
"""

ALL_DDL = [_SCHEMA_DDL, _CONTEXT_DDL, _MOOD_DDL, _FOCUS_DDL, _CORRELATIONS_DDL]


def _init_db(db_path: str) -> None:
    """Create all tables needed by the correlator."""
    conn = sqlite3.connect(db_path)
    for ddl in ALL_DDL:
        conn.executescript(ddl)
    # Insert seed schema version to pass min_days check
    thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    conn.execute(
        "INSERT INTO schema_version (version, description) VALUES (1, 'test seed')"
    )
    # Insert a context row with old timestamp to satisfy min_days_data check
    conn.execute(
        "INSERT INTO context (source, module, key, value) VALUES (?, ?, ?, ?)",
        ("test", "health", "seed", json.dumps({"old": True})),
    )
    # Update the ts to 30 days ago
    conn.execute(
        "UPDATE context SET ts = ? WHERE module = 'health' AND key = 'seed'",
        (thirty_days_ago,),
    )
    conn.commit()
    conn.close()


def _insert_mood(db_path: str, score: int, days_ago: int = 0) -> None:
    """Insert a mood score row for a specific day."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mood (emoji, score, ts) VALUES (?, ?, ?)",
        ("😐", score, ts),
    )
    conn.commit()
    conn.close()


def _insert_focus(db_path: str, state: str, duration_secs: int,
                   days_ago: int = 0) -> None:
    """Insert a focus entry for a specific day."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO focus (state, source, duration_secs, ts) VALUES (?, ?, ?, ?)",
        (state, "gaming_detection", duration_secs, ts),
    )
    conn.commit()
    conn.close()


def _insert_snapshot(db_path: str, metric: str, value: float,
                      days_ago: int = 0) -> None:
    """Insert a metric snapshot for a specific day."""
    date_key = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO metric_snapshots (metric, value, date_key, source)
           VALUES (?, ?, ?, 'test')
           ON CONFLICT (metric, date_key) DO UPDATE SET
             value = excluded.value""",
        (metric, value, date_key),
    )
    conn.commit()
    conn.close()


def _seed_protein_sleep_correlation(db_path: str, n_days: int = 14,
                                      correlation_direction: float = 1.0) -> None:
    """Seed metric_snapshots with strongly correlated protein↔sleep data."""
    import random
    random.seed(42)
    for day in range(n_days):
        days_ago = n_days - day - 1
        protein = 100 + random.uniform(0, 100)
        noise = random.uniform(-0.5, 0.5) * (1 - abs(correlation_direction))
        if correlation_direction > 0:
            sleep = 5.0 + (protein - 100) / 100 * 3.0 * abs(correlation_direction) + noise
        elif correlation_direction < 0:
            sleep = 8.0 - (protein - 100) / 100 * 3.0 * abs(correlation_direction) + noise
        else:
            sleep = random.uniform(5, 8)

        _insert_snapshot(db_path, "protein.grams_daily", round(protein, 1), days_ago=days_ago)
        _insert_snapshot(db_path, "sleep.hours", round(sleep, 2), days_ago=days_ago)

    # Also seed context for the snapshot logic
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO context (source, module, key, value) VALUES (?, ?, ?, ?)",
        ("test", "protein", "daily_summary", json.dumps({"current_grams": 150, "target_grams": 150})),
    )
    conn.execute(
        "INSERT OR REPLACE INTO context (source, module, key, value) VALUES (?, ?, ?, ?)",
        ("test", "health", "sleep", json.dumps({"hours": 7.0, "quality": "good"})),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    """Create a temporary database with schema."""
    path = str(tmp_path / "test_helios.db")
    _init_db(path)
    return path


@pytest.fixture
def correlator(db_path):
    """Create a CorrelationEngine with test config (low min_days for testing)."""
    config = {
        "min_data_points": 5,
        "min_days_data": 1,  # Very low for testing
        "scan_windows": [7, 14],
    }
    return CorrelationEngine(db_path=db_path, config=config)


@pytest.fixture
def correlator_strict(db_path):
    """Create a CorrelationEngine with default (strict) config."""
    return CorrelationEngine(db_path=db_path, config={"min_days_data": 14})


# ===================================================================
# 1. Statistical function tests
# ===================================================================

class TestPearsonR:
    """Test Pearson correlation coefficient calculation."""

    def test_perfect_positive(self):
        xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        ys = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20]
        r, p = _pearson_r(xs, ys)
        assert abs(r - 1.0) < 0.001
        assert p < 0.001

    def test_perfect_negative(self):
        xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        ys = [20, 18, 16, 14, 12, 10, 8, 6, 4, 2]
        r, p = _pearson_r(xs, ys)
        assert abs(r - (-1.0)) < 0.001
        assert p < 0.001

    def test_no_correlation(self):
        xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        ys = [5, 1, 8, 2, 7, 4, 9, 3, 6, 10]
        r, p = _pearson_r(xs, ys)
        assert abs(r) < 0.5

    def test_insufficient_data(self):
        xs = [1, 2, 3]
        ys = [4, 5, 6]
        r, p = _pearson_r(xs, ys)
        assert r == 0.0
        assert p == 1.0

    def test_zero_variance(self):
        xs = [5, 5, 5, 5, 5, 5, 5]
        ys = [1, 2, 3, 4, 5, 6, 7]
        r, p = _pearson_r(xs, ys)
        assert r == 0.0
        assert p == 1.0

    def test_moderate_positive(self):
        xs = [10, 20, 30, 40, 50, 60, 70]
        ys = [15, 25, 28, 45, 55, 58, 75]
        r, p = _pearson_r(xs, ys)
        assert 0.7 < r < 1.0
        assert p < 0.05


class TestClassifyStrength:
    def test_strong(self):
        assert _classify_strength(0.8) == "strong"
        assert _classify_strength(0.55) == "strong"

    def test_moderate(self):
        assert _classify_strength(0.4) == "moderate"
        assert _classify_strength(0.3) == "moderate"

    def test_weak(self):
        assert _classify_strength(0.2) == "weak"
        assert _classify_strength(0.05) == "weak"


class TestClassifyDirection:
    def test_positive(self):
        assert _classify_direction(0.5) == "positive"
        assert _classify_direction(0.01) == "positive"

    def test_negative(self):
        assert _classify_direction(-0.5) == "negative"
        assert _classify_direction(-0.01) == "negative"

    def test_zero(self):
        assert _classify_direction(0.0) == "positive"


# ===================================================================
# 2. Engine initialization tests
# ===================================================================

class TestCorrelationEngineInit:
    def test_creates_tables(self, db_path):
        engine = CorrelationEngine(db_path=db_path)
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('correlations', 'correlation_observations', 'metric_snapshots')"
        ).fetchall()
        assert len(rows) == 3
        conn.close()

    def test_status_returns_config(self, correlator):
        status = correlator.status()
        assert status["module"] == "correlator"
        assert status["known_pairs"] == len(KNOWN_PAIRS)
        assert status["scan_windows"] == [7, 14]
        assert status["min_data_points"] == 5
        assert status["correlations_stored"] == 0
        assert status["snapshots_stored"] == 0

    def test_default_config(self, db_path):
        engine = CorrelationEngine(db_path=db_path, config={})
        assert engine._min_points == 7
        assert engine._strong_threshold == 0.7
        assert engine._significance == 0.05
        assert engine._scan_windows == [7, 14, 28]


# ===================================================================
# 3. Weekly scan tests
# ===================================================================

class TestWeeklyScan:
    def test_scan_with_insufficient_data_returns_empty(self, correlator_strict, db_path):
        """With less than 14 days of data, scan should return empty."""
        _insert_snapshot(db_path, "protein.grams_daily", 120, days_ago=3)
        result = correlator_strict.run_weekly_scan()
        assert result == []

    def test_scan_finds_positive_correlation(self, correlator, db_path):
        """With positively correlated protein↔sleep data, should find correlation."""
        _seed_protein_sleep_correlation(db_path, n_days=14, correlation_direction=1.0)
        correlator._min_days_data = 1

        results = correlator.run_weekly_scan(
            pairs=[("protein.grams_daily", "sleep.hours")],
            windows=[7],
        )

        assert len(results) >= 1
        corr = results[0]
        assert corr["metric_a"] == "protein.grams_daily"
        assert corr["metric_b"] == "sleep.hours"
        assert corr["window_days"] == 7
        assert corr["direction"] == "positive"
        assert corr["n_observations"] > 0

    def test_scan_stores_correlations_in_db(self, correlator, db_path):
        """Scanned correlations should be persisted in the database."""
        _seed_protein_sleep_correlation(db_path, n_days=14)
        correlator._min_days_data = 1
        correlator.run_weekly_scan(
            pairs=[("protein.grams_daily", "sleep.hours")],
            windows=[7],
        )

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM correlations").fetchall()
        conn.close()

        assert len(rows) >= 1

    def test_scan_generates_rule_suggestion_for_strong(self, correlator, db_path):
        """Strong correlations should generate rule suggestions."""
        _seed_protein_sleep_correlation(db_path, n_days=14, correlation_direction=1.0)
        correlator._min_days_data = 1
        correlator._strong_threshold = 0.3  # Lower threshold for testing

        results = correlator.run_weekly_scan(
            pairs=[("protein.grams_daily", "sleep.hours")],
            windows=[7],
        )

        with_rules = [r for r in results if r.get("suggested_rule") is not None]
        assert len(with_rules) >= 1

    def test_scan_no_correlation_no_rule(self, correlator, db_path):
        """Weak/insignificant correlations should not generate rules."""
        import random
        random.seed(123)
        for day in range(14):
            days_ago = 14 - day
            protein = random.uniform(80, 220)
            sleep = random.uniform(5, 9)
            _insert_snapshot(db_path, "protein.grams_daily", round(protein, 1), days_ago=days_ago)
            _insert_snapshot(db_path, "sleep.hours", round(sleep, 2), days_ago=days_ago)

        correlator._min_days_data = 1
        correlator._strong_threshold = 0.9  # Very high threshold

        results = correlator.run_weekly_scan(
            pairs=[("protein.grams_daily", "sleep.hours")],
            windows=[7],
        )

        with_rules = [r for r in results if r.get("suggested_rule") is not None]
        assert len(with_rules) == 0


# ===================================================================
# 4. Data collection tests
# ===================================================================

class TestDataCollection:
    def test_mood_data_fetching(self, correlator, db_path):
        """Mood scores should be fetchable from the mood table."""
        for day in range(10):
            _insert_mood(db_path, score=5 + day % 5, days_ago=9 - day)

        conn = correlator._get_conn()
        data = correlator._fetch_metric_data(conn, "mood.score", 10)
        conn.close()

        assert len(data) >= 5
        for v in data.values():
            assert isinstance(v, float)

    def test_gaming_data_fetching(self, correlator, db_path):
        """Gaming focus data should be fetchable from the focus table."""
        for day in range(10):
            _insert_focus(db_path, "gaming", duration_secs=3600, days_ago=9 - day)
            _insert_focus(db_path, "working", duration_secs=7200, days_ago=9 - day)

        conn = correlator._get_conn()
        data = correlator._fetch_metric_data(conn, "gaming.minutes_daily", 10)
        conn.close()

        assert len(data) >= 5

    def test_snapshot_data_fetching(self, correlator, db_path):
        """Metric snapshot data should be fetchable."""
        for day in range(10):
            _insert_snapshot(db_path, "protein.grams_daily", 100 + day * 5, days_ago=9 - day)

        conn = correlator._get_conn()
        data = correlator._fetch_metric_data(conn, "protein.grams_daily", 10)
        conn.close()

        assert len(data) >= 5

    def test_snapshot_from_context(self, correlator, db_path):
        """snapshot_from_context should extract and store values."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO context (source, module, key, value) VALUES (?, ?, ?, ?)",
            ("test", "protein", "daily_summary", json.dumps({"current_grams": 145, "target_grams": 150})),
        )
        conn.commit()
        conn.close()

        correlator.snapshot_from_context(
            module="protein",
            key="daily_summary",
            metric="protein.grams_daily",
            value_path="current_grams",
        )

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value FROM metric_snapshots WHERE metric = ?",
            ("protein.grams_daily",),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["value"] == 145.0


# ===================================================================
# 5. Correlation retrieval tests
# ===================================================================

class TestGetCorrelations:
    def test_get_top_correlations_empty(self, correlator):
        """Should return empty list when no correlations stored."""
        result = correlator.get_top_correlations()
        assert result == []

    def test_get_top_correlations_with_data(self, correlator, db_path):
        """Should return correlations ordered by strength."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO correlations (metric_a, metric_b, window_days, pearson_r, p_value, strength, direction, n_observations) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("protein.grams_daily", "sleep.hours", 7, 0.85, 0.001, "strong", "positive", 14),
        )
        conn.execute(
            "INSERT INTO correlations (metric_a, metric_b, window_days, pearson_r, p_value, strength, direction, n_observations) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("gaming.minutes_daily", "sleep.hours", 7, -0.6, 0.02, "moderate", "negative", 14),
        )
        conn.execute(
            "INSERT INTO correlations (metric_a, metric_b, window_days, pearson_r, p_value, strength, direction, n_observations) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("activity.minutes_daily", "mood.score", 14, 0.2, 0.5, "weak", "positive", 14),
        )
        conn.commit()
        conn.close()

        result = correlator.get_top_correlations(limit=2, min_strength="moderate")
        assert len(result) == 2
        assert abs(result[0]["pearson_r"]) > abs(result[1]["pearson_r"])

    def test_get_correlation_specific_pair(self, correlator, db_path):
        """Should retrieve a specific metric pair correlation."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO correlations (metric_a, metric_b, window_days, pearson_r, p_value, strength, direction, n_observations) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("protein.grams_daily", "sleep.hours", 7, 0.85, 0.001, "strong", "positive", 14),
        )
        conn.commit()
        conn.close()

        result = correlator.get_correlation("protein.grams_daily", "sleep.hours")
        assert result is not None
        assert result["metric_a"] == "protein.grams_daily"
        assert result["metric_b"] == "sleep.hours"
        assert result["pearson_r"] == 0.85

    def test_get_correlation_nonexistent(self, correlator):
        """Should return None for nonexistent correlation."""
        result = correlator.get_correlation("foo.bar", "baz.qux")
        assert result is None

    def test_get_top_correlations_filter_by_strength(self, correlator, db_path):
        """Should filter by minimum strength."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO correlations (metric_a, metric_b, window_days, pearson_r, p_value, strength, direction, n_observations) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("a", "b", 7, 0.85, 0.001, "strong", "positive", 14),
        )
        conn.execute(
            "INSERT INTO correlations (metric_a, metric_b, window_days, pearson_r, p_value, strength, direction, n_observations) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("c", "d", 7, 0.2, 0.5, "weak", "positive", 14),
        )
        conn.commit()
        conn.close()

        result = correlator.get_top_correlations(min_strength="strong")
        assert len(result) == 1
        assert result[0]["strength"] == "strong"

    def test_get_top_correlations_filter_by_window(self, correlator, db_path):
        """Should filter by window size."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO correlations (metric_a, metric_b, window_days, pearson_r, p_value, strength, direction, n_observations) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("a", "b", 7, 0.85, 0.001, "strong", "positive", 14),
        )
        conn.execute(
            "INSERT INTO correlations (metric_a, metric_b, window_days, pearson_r, p_value, strength, direction, n_observations) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("a", "b", 28, 0.75, 0.005, "strong", "positive", 28),
        )
        conn.commit()
        conn.close()

        result = correlator.get_top_correlations(window=7)
        assert len(result) == 1
        assert result[0]["window_days"] == 7


# ===================================================================
# 6. Approve rule tests
# ===================================================================

class TestApproveCorrelation:
    def test_approve_correlation_rule(self, correlator, db_path):
        """Should approve a correlation rule."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO correlations (metric_a, metric_b, window_days, pearson_r, p_value, strength, direction, n_observations) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("protein.grams_daily", "sleep.hours", 7, 0.85, 0.001, "strong", "positive", 14),
        )
        conn.commit()
        conn.close()

        result = correlator.approve_correlation_rule(1, approved_by="jefferson")
        assert result is True

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT approved, approved_by FROM correlations WHERE id = 1").fetchone()
        conn.close()

        assert row["approved"] == 1
        assert row["approved_by"] == "jefferson"


# ===================================================================
# 7. Briefing formatting tests
# ===================================================================

class TestBriefingFormat:
    def test_format_with_correlations(self, correlator):
        correlations = [
            {
                "metric_a": "protein.grams_daily",
                "metric_b": "sleep.hours",
                "window_days": 7,
                "pearson_r": 0.85,
                "p_value": 0.001,
                "strength": "strong",
                "direction": "positive",
                "n_observations": 14,
            },
            {
                "metric_a": "gaming.minutes_daily",
                "metric_b": "sleep.hours",
                "window_days": 14,
                "pearson_r": -0.65,
                "p_value": 0.02,
                "strength": "moderate",
                "direction": "negative",
                "n_observations": 14,
            },
        ]
        text = correlator.format_briefing_section(correlations)
        assert "grams" in text.lower() or "protein" in text.lower()
        assert "📈" in text
        assert "📉" in text
        assert "r=" in text

    def test_format_empty_correlations(self, correlator):
        text = correlator.format_briefing_section([])
        assert "No significant patterns" in text

    def test_format_direction_indicators(self, correlator):
        pos = [{"metric_a": "a.x", "metric_b": "b.y", "window_days": 7,
                "pearson_r": 0.5, "p_value": 0.01, "strength": "moderate",
                "direction": "positive", "n_observations": 10}]
        neg = [{"metric_a": "a.x", "metric_b": "b.y", "window_days": 7,
                "pearson_r": -0.5, "p_value": 0.01, "strength": "moderate",
                "direction": "negative", "n_observations": 10}]

        pos_text = correlator.format_briefing_section(pos)
        neg_text = correlator.format_briefing_section(neg)
        assert "📈" in pos_text
        assert "📉" in neg_text


# ===================================================================
# 8. Observation storage tests
# ===================================================================

class TestObservations:
    def test_observations_stored(self, correlator, db_path):
        """Pair observations should be stored in correlation_observations table."""
        _seed_protein_sleep_correlation(db_path, n_days=10, correlation_direction=0.8)
        correlator._min_days_data = 1

        correlator.run_weekly_scan(
            pairs=[("protein.grams_daily", "sleep.hours")],
            windows=[7],
        )

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM correlation_observations").fetchall()
        conn.close()

        assert len(rows) >= 1

    def test_observations_deduplicated(self, correlator, db_path):
        """Repeated scans should not duplicate observations for same date."""
        _seed_protein_sleep_correlation(db_path, n_days=14)
        correlator._min_days_data = 1

        correlator.run_weekly_scan(
            pairs=[("protein.grams_daily", "sleep.hours")],
            windows=[7],
        )
        correlator.run_weekly_scan(
            pairs=[("protein.grams_daily", "sleep.hours")],
            windows=[7],
        )

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT metric_a, metric_b, date_key, COUNT(*) as cnt "
            "FROM correlation_observations GROUP BY metric_a, metric_b, date_key "
            "HAVING cnt > 1"
        ).fetchall()
        conn.close()

        assert len(rows) == 0


# ===================================================================
# 9. Rule suggestion tests
# ===================================================================

class TestRuleSuggestion:
    def test_maybe_suggest_rule_strong(self, correlator):
        """Strong significant correlations should generate rule suggestions."""
        corr = {
            "metric_a": "protein.grams_daily",
            "metric_b": "sleep.hours",
            "window_days": 7,
            "pearson_r": 0.85,
            "p_value": 0.001,
            "strength": "strong",
            "direction": "positive",
            "n_observations": 20,
            "is_significant": True,
        }
        rule = correlator._maybe_suggest_rule(corr)
        assert rule is not None
        assert rule["trigger_type"] == "pattern"
        assert "corr_" in rule["slug"]
        assert "protein" in rule["slug"]

    def test_maybe_suggest_rule_weak_returns_none(self, correlator):
        """Weak correlations should not generate rule suggestions."""
        corr = {
            "metric_a": "a.x",
            "metric_b": "b.y",
            "window_days": 7,
            "pearson_r": 0.2,
            "p_value": 0.5,
            "strength": "weak",
            "direction": "positive",
            "n_observations": 20,
            "is_significant": False,
        }
        rule = correlator._maybe_suggest_rule(corr)
        assert rule is None

    def test_maybe_suggest_rule_not_significant(self, correlator):
        """Statistically insignificant strong-looking correlations should not generate rules."""
        corr = {
            "metric_a": "a.x",
            "metric_b": "b.y",
            "window_days": 7,
            "pearson_r": 0.8,
            "p_value": 0.15,
            "strength": "strong",
            "direction": "positive",
            "n_observations": 8,
            "is_significant": False,
        }
        rule = correlator._maybe_suggest_rule(corr)
        assert rule is None


# ===================================================================
# 10. Snapshot tests
# ===================================================================

class TestSnapshots:
    def test_snapshot_metric(self, correlator, db_path):
        """snapshot_metric should store a daily value."""
        correlator.snapshot_metric("protein.grams_daily", 145.0, "2026-04-27")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM metric_snapshots WHERE metric = ?",
            ("protein.grams_daily",),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["value"] == 145.0
        assert row["date_key"] == "2026-04-27"

    def test_snapshot_metric_upsert(self, correlator, db_path):
        """Double snapshot for same day should update, not duplicate."""
        correlator.snapshot_metric("protein.grams_daily", 145.0, "2026-04-27")
        correlator.snapshot_metric("protein.grams_daily", 155.0, "2026-04-27")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM metric_snapshots WHERE metric = ? AND date_key = ?",
            ("protein.grams_daily", "2026-04-27"),
        ).fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0]["value"] == 155.0


# ===================================================================
# 11. End-to-end integration test
# ===================================================================

class TestEndToEnd:
    def test_full_scan_pipeline(self, correlator, db_path):
        """Full pipeline: seed data → scan → store → retrieve → format."""
        _seed_protein_sleep_correlation(db_path, n_days=14, correlation_direction=1.0)
        correlator._min_days_data = 1
        correlator._strong_threshold = 0.3

        results = correlator.run_weekly_scan(
            pairs=[("protein.grams_daily", "sleep.hours")],
            windows=[7],
        )

        assert len(results) >= 1

        top = correlator.get_top_correlations(limit=3)
        assert len(top) >= 1
        assert top[0]["pearson_r"] > 0

        text = correlator.format_briefing_section(top)
        assert len(text) > 0

        status = correlator.status()
        assert status["correlations_stored"] >= 1
        assert status["observations_stored"] >= 1

    def test_scan_with_multiple_windows(self, correlator, db_path):
        """Scan should work across multiple time windows."""
        _seed_protein_sleep_correlation(db_path, n_days=21)
        correlator._min_days_data = 1

        results = correlator.run_weekly_scan(
            pairs=[("protein.grams_daily", "sleep.hours")],
            windows=[7, 14],
        )

        assert len(results) >= 1
        window_days = [r["window_days"] for r in results]
        assert any(w in [7, 14] for w in window_days)