"""Tests for stable JSON exports."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pytest

from helios.module_health import ModuleHealthTracker
from helios.stable_exports import (
    build_latest_status,
    build_context_export,
    build_alerts_recent,
    write_all_exports,
    _overall_health,
    SCHEMA_VERSION,
)


# ── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_db(tmp_path: Path) -> str:
    """Create a fresh in-memory-backed SQLite DB with enough schema."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
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
    # calendar_events
    conn.execute("""
        CREATE TABLE calendar_events (
            id INTEGER PRIMARY KEY,
            date_key TEXT,
            title TEXT,
            start TEXT
        )""")
    # alert_history
    conn.execute("""
        CREATE TABLE alert_history (
            id INTEGER PRIMARY KEY,
            rule_slug TEXT NOT NULL,
            ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            severity TEXT NOT NULL DEFAULT 'info',
            category TEXT NOT NULL DEFAULT 'system',
            message TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 1,
            context TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )""")
    # focus
    conn.execute("""
        CREATE TABLE focus (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            state TEXT,
            duration_secs INTEGER
        )""")
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def seeded_db(fresh_db: str) -> str:
    """Seed with realistic data."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(fresh_db)
    # metric_snapshots
    conn.executemany(
        "INSERT INTO metric_snapshots (date_key, metric, value, source, ts) VALUES (?, ?, ?, ?, ?)",
        [
            (today, "sleep.hours", 7.5, "home_assistant_health", datetime.now(timezone.utc).isoformat()),
            (today, "activity.steps_daily", 11126.0, "home_assistant_health", datetime.now(timezone.utc).isoformat()),
            (today, "mood.score_daily", 7.0, "mood", datetime.now(timezone.utc).isoformat()),
            (today, "health.ha_last_sync_epoch", datetime.now(timezone.utc).timestamp(), "home_assistant_health", datetime.now(timezone.utc).isoformat()),
            (yesterday, "sleep.hours", 6.0, "home_assistant_health", datetime.now(timezone.utc).isoformat()),
        ],
    )
    # calendar_events
    conn.execute("INSERT INTO calendar_events (date_key, title, start) VALUES (?, ?, ?)", (today, "Test meeting", "10:00"))
    # alert_history
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    conn.execute(
        "INSERT INTO alert_history (ts, rule_slug, severity, category, message) VALUES (?, ?, ?, ?, ?)",
        (cutoff.isoformat(), "low_steps", "low", "health", "Only 3000 steps so far"),
    )
    # focus
    conn.execute("INSERT INTO focus (ts, state, duration_secs) VALUES (?, ?, ?)", (datetime.now(timezone.utc).isoformat(), "idle", 300))
    conn.commit()
    conn.close()
    return fresh_db


# ── Tests ─────────────────────────────────────────────────────────────────


def test_build_latest_status_basic(seeded_db: str) -> None:
    """latest_status.json has required keys and schema version."""
    result = build_latest_status(seeded_db)
    assert result["schema_version"] == SCHEMA_VERSION
    assert result["engine"] == "helios"
    assert "generated_at" in result
    assert "last_tick_at" in result
    assert isinstance(result["health"], str)
    assert isinstance(result["modules"], dict)
    assert isinstance(result["today"], dict)
    assert isinstance(result["open_alerts"], list)

    today = result["today"]
    assert today["sleep_hours"] == 7.5
    assert today["steps"] == 11126.0
    assert today["mood"] == 7.0
    assert today["calendar_count"] == 1


def test_build_context_export_basic(seeded_db: str) -> None:
    """context_export.json has required keys and windowed data."""
    result = build_context_export(seeded_db, window_days=7)
    assert result["schema_version"] == SCHEMA_VERSION
    assert result["engine"] == "helios"
    assert result["window_days"] == 7
    assert "metrics" in result
    assert "focus" in result
    assert "mood" in result
    assert "calendar" in result
    assert "health" in result

    # Metrics should have entries keyed by metric name
    assert "sleep.hours" in result["metrics"]
    assert len(result["metrics"]["sleep.hours"]) >= 1

    # Mood keyed by date
    assert today_key() in result["mood"]

    # Health from HA source
    assert "sleep.hours" in result["health"]


def test_build_alerts_recent_basic(seeded_db: str) -> None:
    """alerts_recent.json captures recent alerts."""
    result = build_alerts_recent(seeded_db, window_hours=24)
    assert result["schema_version"] == SCHEMA_VERSION
    assert result["window_hours"] == 24
    assert len(result["alerts"]) == 1
    alert = result["alerts"][0]
    assert alert["slug"] == "low_steps"
    assert alert["severity"] == "low"


def test_write_all_exports_atomically(seeded_db: str, tmp_path: Path, monkeypatch) -> None:
    """write_all_exports writes three JSON files atomically."""
    monkeypatch.setattr(
        "helios.stable_exports.EXPORT_DIR",
        tmp_path,
    )
    # Suppress logger noise by mocking logger
    import logging
    logging.disable(logging.CRITICAL)

    try:
        paths = write_all_exports(seeded_db, health_tracker=None)
        assert paths["latest_status"].exists()
        assert paths["context_export"].exists()
        assert paths["alerts_recent"].exists()

        for name, p in paths.items():
            raw = p.read_text()
            data = json.loads(raw)
            assert data["schema_version"] == SCHEMA_VERSION
    finally:
        logging.disable(logging.NOTSET)


def test_missing_data_not_crashing(fresh_db: str, tmp_path: Path, monkeypatch) -> None:
    """With empty DB exports produce null/empty, not crashes."""
    monkeypatch.setattr("helios.stable_exports.EXPORT_DIR", tmp_path)
    result = build_latest_status(fresh_db)
    assert result["today"]["sleep_hours"] is None
    assert result["today"]["steps"] is None
    assert result["today"]["calendar_count"] == 0
    assert result["open_alerts"] == []

    ctx = build_context_export(fresh_db)
    assert ctx["metrics"] == {}
    assert ctx["mood"] == {}
    assert ctx["health"] == {}

    recent = build_alerts_recent(fresh_db)
    assert recent["alerts"] == []


def test_overall_health_aggregation() -> None:
    assert _overall_health(["healthy", "healthy"]) == "healthy"
    assert _overall_health(["healthy", "stale"]) == "stale"
    assert _overall_health(["stale", "degraded"]) == "degraded"
    assert _overall_health(["failed", "healthy"]) == "failed"
    assert _overall_health([]) == "unknown"


# ── Helpers ───────────────────────────────────────────────────────────────


def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
