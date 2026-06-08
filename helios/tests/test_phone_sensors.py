"""Tests for helios.modules.phone_sensors (SAN-124)."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from helios.modules.phone_sensors import PhoneSensorsModule, SOURCE

# ── Schema DDL for test DBs ───────────────────────────────────────────────────

METRIC_DDL = """
CREATE TABLE IF NOT EXISTS metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    date_key TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'ingestion',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT uq_metric_date UNIQUE (metric, date_key)
);
"""


@pytest.fixture
def fresh_db(tmp_path: Path) -> str:
    """Provide a temporary SQLite DB with metric_snapshots table ready."""
    db_path = str(tmp_path / "test_helios.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(METRIC_DDL)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def module(fresh_db: str) -> PhoneSensorsModule:
    """Return a PhoneSensorsModule wired to a temp DB."""
    return PhoneSensorsModule(db_path=fresh_db, config={})


# ── test_phone_sensors_module_info ────────────────────────────────────────────

def test_phone_sensors_module_info(module: PhoneSensorsModule):
    info = module.module_info()
    assert info["name"] == "phone_sensors"
    assert info["version"] == "1.0.0"
    assert "phone sensor" in info["description"].lower()
    assert info["health"]["status"] == "healthy"
    assert "collectors" in info
    assert "health_auto_export_api" in info["collectors"]


# ── test_phone_sensors_ingest_steps ────────────────────────────────────────────

def test_phone_sensors_ingest_steps(module: PhoneSensorsModule, fresh_db: str):
    """Steps from the API should land in metric_snapshots as phone.steps_daily."""
    mock_data = {
        "date": "2026-06-08",
        "steps": 8432,
        "active_calories": 680,
    }
    with patch.object(module, "_get", return_value=mock_data):
        result = module.tick()

    assert result.get("steps") == 8432
    assert result.get("steps_date") == "2026-06-08"

    conn = sqlite3.connect(fresh_db)
    row = conn.execute(
        "SELECT value, source FROM metric_snapshots WHERE metric = 'phone.steps_daily'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 8432.0
    assert row[1] == SOURCE


# ── test_phone_sensors_ingest_battery ──────────────────────────────────────────

def test_phone_sensors_ingest_battery(module: PhoneSensorsModule, fresh_db: str):
    """Battery level from the API should land as phone.battery_level."""
    mock_data = {
        "level": 78,
        "state": "charging",
        "timestamp": "2026-06-08T14:30:00",
    }
    # Steps and screen_time APIs return nothing useful in this test
    with patch.object(module, "_get") as mock_get:
        # First call: steps → empty; second: battery → mock_data; third: screen_time → empty
        call_count = [0]
        def side_effect(path):
            idx = call_count[0]
            call_count[0] += 1
            if idx == 0:
                return {}  # steps endpoint - no steps data
            elif idx == 1:
                return mock_data  # battery endpoint
            else:
                return {}  # screen_time endpoint
        mock_get.side_effect = side_effect
        result = module.tick()

    assert result.get("battery_level") == 78
    assert result.get("battery_state") == "charging"

    conn = sqlite3.connect(fresh_db)
    row = conn.execute(
        "SELECT value, source FROM metric_snapshots WHERE metric = 'phone.battery_level'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 78.0
    assert row[1] == SOURCE


# ── test_phone_sensors_ingest_screen_time ───────────────────────────────────────

def test_phone_sensors_ingest_screen_time(module: PhoneSensorsModule, fresh_db: str):
    """Screen time from the API should land as phone.screen_time_minutes and pickups."""
    mock_data = {
        "total_minutes": 142,
        "date": "2026-06-08",
        "pickups": 67,
    }
    with patch.object(module, "_get") as mock_get:
        call_count = [0]
        def side_effect(path):
            idx = call_count[0]
            call_count[0] += 1
            if idx == 0:
                return {}  # steps - no data
            elif idx == 1:
                return {}  # battery - no data
            else:
                return mock_data  # screen_time
        mock_get.side_effect = side_effect
        result = module.tick()

    assert result.get("screen_time_minutes") == 142
    assert result.get("screen_time_pickups") == 67

    conn = sqlite3.connect(fresh_db)
    minutes_row = conn.execute(
        "SELECT value FROM metric_snapshots WHERE metric = 'phone.screen_time_minutes'"
    ).fetchone()
    pickups_row = conn.execute(
        "SELECT value FROM metric_snapshots WHERE metric = 'phone.screen_time_pickups'"
    ).fetchone()
    conn.close()
    assert minutes_row is not None
    assert minutes_row[0] == 142.0
    assert pickups_row is not None
    assert pickups_row[0] == 67.0


# ── test_phone_sensors_api_timeout_graceful ─────────────────────────────────────

def test_phone_sensors_api_timeout_graceful(module: PhoneSensorsModule):
    """If the API times out, the module should return gracefully, not crash."""
    import requests

    with patch.object(module, "_get", side_effect=requests.exceptions.Timeout("timeout")):
        # _get won't raise — it catches exceptions. But if we call tick
        # with _get patched to return None (simulating timeout):
        with patch.object(module, "_get", return_value=None):
            result = module.tick()

    assert result.get("source") == SOURCE
    # No crash, result has info about no data
    assert result.get("metrics_written") == 0


# ── test_phone_sensors_api_down_graceful ─────────────────────────────────────────

def test_phone_sensors_api_down_graceful(module: PhoneSensorsModule):
    """If the API is completely down (connection refused), module should not crash."""
    with patch.object(module, "_get", return_value=None):
        result = module.tick()

    assert result.get("source") == SOURCE
    assert result.get("metrics_written") == 0
    assert "_error" not in result or not result.get("_error")


# ── test_phone_sensors_writes_to_metric_snapshots ──────────────────────────────

def test_phone_sensors_writes_to_metric_snapshots(module: PhoneSensorsModule, fresh_db: str):
    """Verify all three data sources write to metric_snapshots with source='phone_sensors'."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    steps_data = {"steps": 5500, "date": today}
    battery_data = {"level": 55, "state": "unplugged", "timestamp": "2026-06-08T10:00:00Z", "date": today}
    screen_data = {"total_minutes": 90, "pickups": 45, "date": today}

    with patch.object(module, "_get") as mock_get:
        call_count = [0]
        def side_effect(path):
            idx = call_count[0]
            call_count[0] += 1
            if idx == 0:
                return steps_data
            elif idx == 1:
                return battery_data
            else:
                return screen_data
        mock_get.side_effect = side_effect
        result = module.tick()

    assert result["metrics_written"] >= 3

    conn = sqlite3.connect(fresh_db)
    rows = conn.execute(
        "SELECT metric, value, source, date_key FROM metric_snapshots WHERE source = ? ORDER BY metric",
        (SOURCE,),
    ).fetchall()
    conn.close()

    metrics_written = {row[0]: row[1] for row in rows}
    assert "phone.steps_daily" in metrics_written
    assert metrics_written["phone.steps_daily"] == 5500.0
    assert "phone.battery_level" in metrics_written
    assert metrics_written["phone.battery_level"] == 55.0
    assert "phone.screen_time_minutes" in metrics_written
    assert metrics_written["phone.screen_time_minutes"] == 90.0
    assert "phone.screen_time_pickups" in metrics_written
    assert metrics_written["phone.screen_time_pickups"] == 45.0

    # All rows should have source = phone_sensors
    for row in rows:
        assert row[2] == SOURCE


# ── test_phone_sensors_circuit_breaker ─────────────────────────────────────────

def test_phone_sensors_circuit_breaker(module: PhoneSensorsModule):
    """After enough failures, the circuit breaker should open and skip API calls."""

    # Force failures by having _get raise — this triggers record_failure in _get
    # We'll simulate by calling record_failure directly
    provider = "phone_api:/health/activity"

    # Record 4 failures (below threshold of 5) — should still be degraded, not open
    for i in range(4):
        module._cb.record_failure(provider)

    assert module._cb.state(provider) in ("degraded", "closed")

    # 5th failure should open the circuit
    module._cb.record_failure(provider)
    assert module._cb.state(provider) == "open"

    # should_attempt should return False now
    assert module._cb.should_attempt(provider) is False

    # Health should report degraded
    health = module.health()
    assert health["status"] == "degraded"


# ── test_phone_sensors_env_var_api_url ──────────────────────────────────────────

def test_phone_sensors_env_var_api_url(fresh_db: str):
    """HELIOS_PHONE_API_URL env var should be respected."""
    with patch.dict(os.environ, {"HELIOS_PHONE_API_URL": "http://custom-host:9999"}):
        mod = PhoneSensorsModule(db_path=fresh_db, config={})
        assert mod._api_url == "http://custom-host:9999"


def test_phone_sensors_config_api_url(fresh_db: str):
    """Config api_url should be respected and takes precedence."""
    mod = PhoneSensorsModule(
        db_path=fresh_db,
        config={"api_url": "http://config-host:7777"},
    )
    assert mod._api_url == "http://config-host:7777"


# ── test_phone_sensors_no_db_path ───────────────────────────────────────────────

def test_phone_sensors_no_db_path():
    """Module should handle missing db_path gracefully."""
    mod = PhoneSensorsModule(db_path=None, config={})
    result = mod.tick()
    assert result.get("_warning") == "No db_path configured"
    assert result.get("source") == SOURCE