"""Tests for insight engine export validation."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pytest

from helios.insight_engine import (
    generate_all_insights,
    INSIGHT_SCHEMA_VERSION,
    explore_timeline,
    compute_trends,
    explore_correlations,
    diff_narratives,
    trace_evidence,
)


# ── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def insight_db(tmp_path: Path) -> str:
    """Create a DB with the schema insight_engine queries require."""
    db_path = tmp_path / "insight_test.db"
    conn = sqlite3.connect(str(db_path))

    # timeline_events
    conn.execute("""
        CREATE TABLE timeline_events (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            event_type TEXT,
            source_module TEXT,
            importance REAL,
            summary TEXT,
            date_key TEXT,
            metadata TEXT
        )""")
    # timeline_sessions
    conn.execute("""
        CREATE TABLE timeline_sessions (
            id INTEGER PRIMARY KEY,
            session_type TEXT,
            date_key TEXT,
            session_start TEXT,
            session_end TEXT,
            duration_secs REAL,
            dominant_state TEXT,
            event_count INTEGER,
            source_events TEXT,
            summary TEXT,
            metadata TEXT,
            confidence REAL,
            importance REAL,
            novelty REAL,
            created_at TEXT
        )""")
    # event_links
    conn.execute("""
        CREATE TABLE event_links (
            id INTEGER PRIMARY KEY,
            source_event_id INTEGER,
            target_event_id INTEGER,
            link_type TEXT,
            strength REAL
        )""")
    # metric_snapshots
    conn.execute("""
        CREATE TABLE metric_snapshots (
            id INTEGER PRIMARY KEY,
            date_key TEXT,
            metric TEXT,
            value REAL,
            source TEXT,
            ts TEXT
        )""")
    # correlations
    conn.execute("""
        CREATE TABLE correlations (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            metric_a TEXT,
            metric_b TEXT,
            window_days INTEGER,
            pearson_r REAL,
            p_value REAL,
            strength TEXT,
            direction TEXT,
            n_observations INTEGER,
            suggested_rule TEXT,
            approved INTEGER,
            approved_by TEXT,
            created_at TEXT,
            updated_at TEXT
        )""")
    # metric_anomalies (referenced by explore_timeline)
    conn.execute("""
        CREATE TABLE metric_anomalies (
            id INTEGER PRIMARY KEY,
            date_key TEXT,
            metric TEXT,
            detected_value REAL,
            expected_value REAL,
            deviation_std REAL,
            direction TEXT,
            confidence REAL,
            ts TEXT,
            resolved INTEGER
        )""")
    # narrative_statements
    conn.execute("""
        CREATE TABLE narrative_statements (
            id INTEGER PRIMARY KEY,
            date_key TEXT,
            statement TEXT,
            confidence REAL,
            source TEXT,
            evidence_types TEXT,
            ts TEXT
        )""")
    # notable_events (referenced by trace_evidence)
    conn.execute("""
        CREATE TABLE notable_events (
            id INTEGER PRIMARY KEY,
            date_key TEXT,
            rank INTEGER,
            event_type TEXT,
            session_id INTEGER,
            timeline_event_id INTEGER,
            summary TEXT,
            importance REAL,
            novelty REAL,
            confidence REAL,
            created_at TEXT
        )""")
    # focus_daily_summary (referenced by compute_trends)
    conn.execute("""
        CREATE TABLE focus_daily_summary (
            id INTEGER PRIMARY KEY,
            date_key TEXT,
            state TEXT,
            total_secs INTEGER,
            session_count INTEGER,
            first_seen TEXT,
            last_seen TEXT
        )""")
    # correlation_observations (referenced by explore_correlations and trace_evidence)
    conn.execute("""
        CREATE TABLE correlation_observations (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            metric_a TEXT,
            metric_b TEXT,
            value_a REAL,
            value_b REAL,
            date_key TEXT,
            created_at TEXT
        )""")
    # alert_history
    conn.execute("""
        CREATE TABLE alert_history (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            slug TEXT,
            severity TEXT,
            title TEXT,
            message TEXT,
            delivered INTEGER,
            resolved INTEGER
        )""")

    today = datetime.now(timezone.utc)
    today_str = today.strftime("%Y-%m-%d")
    week_ago = (today - timedelta(days=6)).strftime("%Y-%m-%d")

    # Seed timeline events
    conn.executemany(
        "INSERT INTO timeline_events (ts, event_type, source_module, importance, summary, date_key) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (today.isoformat(), "sleep", "health", 0.8, "Slept 7 hours", today_str),
            ((today - timedelta(days=3)).isoformat(), "gaming", "focus", 0.6, "Gaming session", (today - timedelta(days=3)).strftime("%Y-%m-%d")),
        ],
    )

    # Seed timeline sessions
    conn.executemany(
        "INSERT INTO timeline_sessions (date_key, session_type, summary, importance, novelty, duration_secs, source_events, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (today_str, "sleep", "Sleep session", 0.8, 0.3, 25200, "[1]", today.isoformat()),
            ((today - timedelta(days=3)).strftime("%Y-%m-%d"), "gaming", "Gaming session", 0.5, 0.6, 7200, "[2]", today.isoformat()),
        ],
    )

    # Seed metric_snapshots
    conn.executemany(
        "INSERT INTO metric_snapshots (date_key, metric, value, source, ts) VALUES (?, ?, ?, ?, ?)",
        [
            (today_str, "sleep.hours", 7.0, "home_assistant_health", today.isoformat()),
            ((today - timedelta(days=7)).strftime("%Y-%m-%d"), "sleep.hours", 6.5, "home_assistant_health", (today - timedelta(days=7)).isoformat()),
            (today_str, "activity.steps_daily", 10000.0, "home_assistant_health", today.isoformat()),
            ((today - timedelta(days=7)).strftime("%Y-%m-%d"), "activity.steps_daily", 8000.0, "home_assistant_health", (today - timedelta(days=7)).isoformat()),
        ],
    )

    # Seed correlations
    conn.execute(
        "INSERT INTO correlations (ts, metric_a, metric_b, window_days, pearson_r, p_value, strength, direction, n_observations, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (today.isoformat(), "sleep.hours", "activity.steps_daily", 14, 0.65, 0.05, "moderate", "positive", 10, today.isoformat(), today.isoformat()),
    )

    # Seed anomalies (must exist for explore_timeline)
    conn.execute(
        "INSERT INTO metric_anomalies (date_key, metric, detected_value, direction, confidence, ts, resolved) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (today_str, "sleep.hours", 4.0, "low", 0.9, today.isoformat(), 0),
    )

    # Seed narratives
    conn.executemany(
        "INSERT INTO narrative_statements (date_key, statement, confidence, source, evidence_types, ts) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (today_str, "Sleep improved", 0.7, "health", "metric", today.isoformat()),
            (week_ago, "Activity was low", 0.6, "focus", "metric", week_ago),
        ],
    )

    # Seed notable_events (for trace_evidence)
    conn.executemany(
        "INSERT INTO notable_events (date_key, rank, event_type, session_id, timeline_event_id, summary, importance, novelty, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (today_str, 1, "sleep", 1, 1, "Notable sleep event", 0.8, 0.3, 0.9, today.isoformat()),
            ((today - timedelta(days=3)).strftime("%Y-%m-%d"), 2, "gaming", 2, 2, "Notable gaming event", 0.6, 0.5, 0.7, today.isoformat()),
        ],
    )

    # Seed focus_daily_summary
    conn.executemany(
        "INSERT INTO focus_daily_summary (date_key, state, total_secs, session_count, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (today_str, "idle", 3600, 3, today.isoformat(), today.isoformat()),
            (week_ago, "active", 7200, 5, week_ago, week_ago),
        ],
    )

    # Seed alerts
    conn.execute(
        "INSERT INTO alert_history (ts, slug, severity, title, message, delivered, resolved) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (today.isoformat(), "test_alert", "low", "Test alert", "Test message", 1, 0),
    )

    conn.commit()
    conn.close()
    return str(db_path)


# ── Tests ─────────────────────────────────────────────────────────────────


def test_contracts_json_serializable() -> None:
    """ALL_CONTRACTS must be JSON serializable."""
    from helios.insight_engine import ALL_CONTRACTS
    raw = json.dumps(ALL_CONTRACTS, indent=2)
    data = json.loads(raw)
    assert "timeline_explorer" in data
    assert "trend" in data
    assert "correlation" in data
    assert "narrative_diff" in data
    assert "evidence" in data


def test_explore_timeline_returns_valid_dict(insight_db: str) -> None:
    """timeline explorer returns a dict with expected keys."""
    conn = sqlite3.connect(insight_db)
    tl = explore_timeline(conn, window_days=7)
    conn.close()
    assert isinstance(tl, dict)
    assert "event_count" in tl
    assert "events" in tl


def test_compute_trends_returns_valid_dict(insight_db: str) -> None:
    """trend engine returns a dict with trends list."""
    conn = sqlite3.connect(insight_db)
    trends = compute_trends(conn)
    conn.close()
    assert isinstance(trends, dict)
    assert "trends" in trends
    assert isinstance(trends["trends"], list)


def test_explore_correlations_returns_valid_dict(insight_db: str) -> None:
    """correlation explorer returns a dict with count."""
    conn = sqlite3.connect(insight_db)
    corr = explore_correlations(conn)
    conn.close()
    assert isinstance(corr, dict)
    assert "count" in corr
    assert isinstance(corr["correlations"], list)


def test_diff_narratives_returns_valid_dict(insight_db: str) -> None:
    """narrative diff returns a dict with expected keys."""
    conn = sqlite3.connect(insight_db)
    diff = diff_narratives(conn)
    conn.close()
    assert isinstance(diff, dict)
    assert "change_count" in diff


def test_trace_evidence_returns_valid_dict(insight_db: str) -> None:
    """evidence tracer returns a dict with expected keys."""
    conn = sqlite3.connect(insight_db)
    ev = trace_evidence(conn)
    conn.close()
    assert isinstance(ev, dict)
    assert "trace_count" in ev


def test_generate_all_insights_writes_valid_json(insight_db: str, tmp_path: Path, monkeypatch) -> None:
    """generate_all_insights writes all five JSON exports + contracts, all valid."""
    monkeypatch.setattr("helios.insight_engine.INSIGHT_DIR", tmp_path)

    conn = sqlite3.connect(insight_db)
    result = generate_all_insights(conn, window_days=7)
    conn.close()

    # Check result summary
    assert "generated_at" in result
    assert "timeline_explorer" in result
    assert "trends" in result
    assert "correlations" in result
    assert "narrative_diff" in result
    assert "evidence" in result

    # Check files exist and are valid JSON
    expected_files = [
        "timeline_explorer.json",
        "trends.json",
        "correlations.json",
        "narrative_diff.json",
        "evidence_traces.json",
        "_contracts.json",
    ]

    for fname in expected_files:
        fpath = tmp_path / fname
        assert fpath.exists(), f"{fname} not written"
        raw = fpath.read_text()
        data = json.loads(raw)  # must not raise
        assert "schema_version" in data, f"{fname} missing schema_version"
        assert data["schema_version"] == INSIGHT_SCHEMA_VERSION