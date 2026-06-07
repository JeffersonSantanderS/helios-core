"""Helios v7 — Test SchedulerStore and scheduler compatibility."""
import json
import os
import tempfile

import pytest

from helios.scheduler import Scheduler, SchedulerStore
from helios.state import HeliosDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    """Return a temp database path, ensuring the schema is applied."""
    path = str(tmp_path / "test_scheduler.db")
    # Initialise the full schema so that scheduled_jobs / job_runs exist
    HeliosDB(db_path=path)
    return path


@pytest.fixture
def store(db_path):
    """Return a SchedulerStore backed by a temp database."""
    s = SchedulerStore(db_path)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# test_scheduler_store_ensure_job_creates
# ---------------------------------------------------------------------------

def test_scheduler_store_ensure_job_creates(store: SchedulerStore):
    """ensure_job inserts a new row; calling again updates cadence/timezone."""
    store.ensure_job("morning_briefing", "daily 07:00", "America/Edmonton")
    store.ensure_job("evening_debrief", "daily 21:00", "America/Edmonton")

    conn = store._connect()
    rows = conn.execute(
        "SELECT job_key, cadence, timezone FROM scheduled_jobs ORDER BY job_key"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["job_key"] == "evening_debrief"
    assert rows[1]["job_key"] == "morning_briefing"

    # Ensure is idempotent — re-upsert updates cadence
    store.ensure_job("morning_briefing", "daily 08:00", "UTC")
    row = conn.execute(
        "SELECT cadence, timezone FROM scheduled_jobs WHERE job_key=?",
        ("morning_briefing",),
    ).fetchone()
    assert row["cadence"] == "daily 08:00"
    assert row["timezone"] == "UTC"


# ---------------------------------------------------------------------------
# test_scheduler_store_mark_due
# ---------------------------------------------------------------------------

def test_scheduler_store_mark_due(store: SchedulerStore):
    """mark_due sets the last_due_at timestamp on a registered job."""
    store.ensure_job("brain_tick", "interval 3600")
    store.mark_due("brain_tick", "2026-01-15T07:00:00+00:00")

    conn = store._connect()
    row = conn.execute(
        "SELECT last_due_at FROM scheduled_jobs WHERE job_key=?",
        ("brain_tick",),
    ).fetchone()
    assert row["last_due_at"] == "2026-01-15T07:00:00+00:00"


# ---------------------------------------------------------------------------
# test_scheduler_store_start_complete_run
# ---------------------------------------------------------------------------

def test_scheduler_store_start_complete_run(store: SchedulerStore):
    """start_run creates a job_runs row; complete_run fills completed_at/status."""
    store.ensure_job("daily_briefing", "daily 07:00")

    run_id = store.start_run("daily_briefing", due_at="2026-01-15T07:00:00+00:00")
    assert isinstance(run_id, int)
    assert run_id > 0

    conn = store._connect()
    row = conn.execute(
        "SELECT status, started_at FROM job_runs WHERE id=?", (run_id,)
    ).fetchone()
    assert row["status"] == "running"
    assert row["started_at"] is not None

    # Complete the run
    store.complete_run(run_id, status="ok")

    row = conn.execute(
        "SELECT status, completed_at FROM job_runs WHERE id=?", (run_id,)
    ).fetchone()
    assert row["status"] == "ok"
    assert row["completed_at"] is not None

    # Scheduled-jobs summary should also be updated
    job = conn.execute(
        "SELECT last_status FROM scheduled_jobs WHERE job_key=?",
        ("daily_briefing",),
    ).fetchone()
    assert job["last_status"] == "ok"


def test_scheduler_store_start_run_with_metadata(store: SchedulerStore):
    """start_run serialises the metadata dict as JSON."""
    store.ensure_job("brain_tick", "interval 3600")
    run_id = store.start_run("brain_tick", metadata={"attempts": 3})

    conn = store._connect()
    row = conn.execute(
        "SELECT metadata_json FROM job_runs WHERE id=?", (run_id,)
    ).fetchone()
    assert json.loads(row["metadata_json"]) == {"attempts": 3}


def test_scheduler_store_complete_run_with_error(store: SchedulerStore):
    """complete_run records error status and message."""
    store.ensure_job("broken_job", "daily 09:00")
    run_id = store.start_run("broken_job")
    store.complete_run(run_id, status="error", error="connection timeout")

    conn = store._connect()
    row = conn.execute(
        "SELECT status, error FROM job_runs WHERE id=?", (run_id,)
    ).fetchone()
    assert row["status"] == "error"
    assert row["error"] == "connection timeout"

    job = conn.execute(
        "SELECT last_status, last_error FROM scheduled_jobs WHERE job_key=?",
        ("broken_job",),
    ).fetchone()
    assert job["last_status"] == "error"
    assert job["last_error"] == "connection timeout"


# ---------------------------------------------------------------------------
# test_scheduler_store_last_completed
# ---------------------------------------------------------------------------

def test_scheduler_store_last_completed(store: SchedulerStore):
    """last_completed returns the timestamp of the most recent successful run."""
    store.ensure_job("metrics_rollup", "hourly")

    # Before any run
    assert store.last_completed("metrics_rollup") is None

    # Start and complete a run
    run_id = store.start_run("metrics_rollup")
    store.complete_run(run_id, status="ok")

    ts = store.last_completed("metrics_rollup")
    assert ts is not None
    # Should be an ISO-8601-ish string
    assert "T" in ts

    # None for unknown job key
    assert store.last_completed("nonexistent") is None


# ---------------------------------------------------------------------------
# test_config_loader_scheduler_compat
# ---------------------------------------------------------------------------

def test_config_loader_scheduler_compat():
    """Scheduler uses segmented keys (cfg.get('scheduler', 'brain_interval'))."""
    from pathlib import Path
    from helios.config_loader import ConfigLoader

    cfg = ConfigLoader(
        {"scheduler": {"brain_interval": 1800, "daily_briefing": "06:30"}},
        Path("dummy"),
    )

    # Segmented key access — the canonical API
    assert cfg.get("scheduler", "brain_interval") == 1800
    assert cfg.get("scheduler", "daily_briefing") == "06:30"

    # Dotted-key compat still works
    assert cfg.get("scheduler.brain_interval") == 1800
    assert cfg.get("scheduler.daily_briefing") == "06:30"

    # Scheduler constructed with config loader works
    sched = Scheduler(cfg)
    assert sched.should_run_brain(None) is True