"""Helios v5 — Scheduler.

Determines what should run on this tick.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("helios.scheduler")


class Scheduler:
    def __init__(self, cfg: Any):
        self.cfg = cfg

    def should_run_brain(self, last_brain_ts: Optional[str]) -> bool:
        interval = self.cfg.get("scheduler", "brain_interval", default=3600)
        if not last_brain_ts:
            return True
        try:
            delta = (datetime.now(timezone.utc) - datetime.fromisoformat(last_brain_ts)).total_seconds()
            return delta >= interval
        except Exception:
            return True

    def should_run_daily_briefing(self, last_briefing_ts: Optional[str]) -> bool:
        schedule_time = self.cfg.get("scheduler", "daily_briefing", default="07:00")
        now = datetime.now(timezone.utc)
        if not last_briefing_ts:
            return True
        try:
            last = datetime.fromisoformat(last_briefing_ts)
            return now.date() > last.date() and now.strftime("%H:%M") >= schedule_time
        except Exception:
            return True

    def should_run_evening_debrief(self, last_debrief_ts: Optional[str]) -> bool:
        schedule_time = "21:00"
        now = datetime.now(timezone.utc)
        if not last_debrief_ts:
            return True
        try:
            last = datetime.fromisoformat(last_debrief_ts)
            return now.date() > last.date() and now.strftime("%H:%M") >= schedule_time
        except Exception:
            return True

    def what_to_run(self, state: dict) -> list[str]:
        tasks = ["tick"]
        if self.should_run_brain(state.get("last_brain")):
            tasks.append("brain")
        if self.should_run_daily_briefing(state.get("last_briefing")):
            tasks.append("daily_briefing")
        if self.should_run_evening_debrief(state.get("last_debrief")):
            tasks.append("evening_debrief")
        return tasks


class SchedulerStore:
    """Durable state for scheduled jobs backed by SQLite.

    Uses safe connection settings (WAL journal, busy timeout) and provides
    a minimal API for registering jobs, tracking due times, and recording
    individual runs.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            try:
                self._conn.execute("SELECT 1")
                return self._conn
            except sqlite3.ProgrammingError:
                self._conn = None

        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── job registry ──────────────────────────────────────────────

    def ensure_job(self, job_key: str, cadence: str, timezone: str = "America/Edmonton") -> None:
        """Insert a job definition if it does not already exist."""
        conn = self._connect()
        conn.execute(
            """INSERT INTO scheduled_jobs (job_key, cadence, timezone)
               VALUES (?, ?, ?)
               ON CONFLICT(job_key) DO UPDATE SET
                 cadence=excluded.cadence,
                 timezone=excluded.timezone""",
            (job_key, cadence, timezone),
        )
        conn.commit()

    # ── due-time tracking ─────────────────────────────────────────

    def mark_due(self, job_key: str, due_at: str) -> None:
        """Record that a job is due at *due_at* (ISO-8601)."""
        conn = self._connect()
        conn.execute(
            """UPDATE scheduled_jobs
               SET last_due_at=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE job_key=?""",
            (due_at, job_key),
        )
        conn.commit()

    # ── run tracking ──────────────────────────────────────────────

    def start_run(self, job_key: str, due_at: Optional[str] = None, metadata: Optional[dict] = None) -> int:
        """Start a new run for *job_key* and return the run id."""
        conn = self._connect()
        now = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata) if metadata else None
        cur = conn.execute(
            """INSERT INTO job_runs (job_key, due_at, started_at, status, metadata_json)
               VALUES (?, ?, ?, 'running', ?)""",
            (job_key, due_at, now, meta_json),
        )
        conn.execute(
            """UPDATE scheduled_jobs
               SET last_started_at=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE job_key=?""",
            (now, job_key),
        )
        run_id: int = cur.lastrowid  # type: ignore[assignment]
        conn.commit()
        return run_id

    def complete_run(self, run_id: int, status: str, error: Optional[str] = None) -> None:
        """Mark a run as finished with *status* ('ok', 'error', …)."""
        conn = self._connect()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE job_runs
               SET completed_at=?, status=?, error=?
               WHERE id=?""",
            (now, status, error, run_id),
        )
        conn.execute(
            """UPDATE scheduled_jobs
               SET last_completed_at=?, last_status=?, last_error=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE job_key=(SELECT job_key FROM job_runs WHERE id=?)""",
            (now, status, error, run_id),
        )
        conn.commit()

    # ── queries ────────────────────────────────────────────────────

    def last_completed(self, job_key: str) -> Optional[str]:
        """Return the ISO-8601 timestamp of the last completed run, or None."""
        conn = self._connect()
        row = conn.execute(
            "SELECT last_completed_at FROM scheduled_jobs WHERE job_key=?",
            (job_key,),
        ).fetchone()
        if row and row["last_completed_at"]:
            return row["last_completed_at"]
        return None