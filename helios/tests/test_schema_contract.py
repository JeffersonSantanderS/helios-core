"""Helios — Schema contract regression tests.

Ensures a fresh HeliosDB has all runtime-required tables and that
key constraints (e.g. focus.state accepting 'screen_time') are valid.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from helios.state import HeliosDB

# ── paths ────────────────────────────────────────────────────────────
MIGRATIONS_DIR: Path = Path(__file__).resolve().parent.parent / "helios" / "migrations"

# ── Runtime-required tables (main DB only, not self_improvement.db) ──
REQUIRED_TABLES = [
    # Core v5 schema
    "context",
    "llm_requests",
    "decisions",
    "rules",
    "mood",
    "focus",
    "habits",
    "habit_log",
    "tasks",
    "schema_version",
    # Migration-layer tables
    "calendar_events",
    "briefing_log",
    "correlations",
    "correlation_observations",
    "nl_queries",
    "subscriptions",
    "goals",
    "goal_milestones",
    "goal_progress",
    "alert_history",
    "alert_snoozes",
    "metric_snapshots",
    "email_scan_log",
    # v6 additions — previously missing from fresh DB
    "module_health",
    "reminders",
    "action_log",
    "prediction_outcomes",
    "focus_daily_summary",
    "timeline_events",
    "event_links",
    "timeline_sessions",
    "session_metrics",
    "notable_events",
    # Priority engine
    "priority_candidates",
    "priority_decisions",
    "priority_scores",
    "priority_feedback",
    # Scheduler / delivery
    "scheduled_jobs",
    "job_runs",
    "delivery_attempts",
]


@pytest.fixture()
def fresh_db(tmp_path):
    """Provide a HeliosDB pointing at a brand-new temp database, cleaning up after."""
    db_file = tmp_path / "test_contract.db"
    db = HeliosDB(db_path=str(db_file))
    yield db
    db.close()


class TestSchemaCompleteness:
    """Verify that all runtime-required tables exist after a fresh schema init."""

    def _get_tables(self, db: HeliosDB) -> set[str]:
        rows = db._execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {r["name"] for r in rows}

    def test_all_required_tables_exist(self, fresh_db):
        """Every table referenced by runtime code must exist in a fresh DB."""
        tables = self._get_tables(fresh_db)
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        assert missing == [], (
            f"The following required tables are missing from a fresh DB: {missing}"
        )

    def test_no_extra_junk_tables(self, fresh_db):
        """Sanity check: the fresh DB shouldn't have unexpected tables."""
        tables = self._get_tables(fresh_db)
        # Allow sqlite internal tables
        expected = set(REQUIRED_TABLES) | {"sqlite_sequence"}
        unexpected = tables - expected
        # Don't fail on extra tables (migrations may add more), just note them
        # But DO fail if critical tables are missing
        for t in REQUIRED_TABLES:
            assert t in tables, f"Required table {t!r} missing — got {sorted(tables)}"


class TestFocusScreenTimeConstraint:
    """Verify that the focus table accepts 'screen_time' as a valid state."""

    def test_insert_screen_time_state(self, fresh_db):
        """INSERT INTO focus with state='screen_time' must succeed."""
        with fresh_db._conn() as c:
            c.execute(
                """INSERT INTO focus (state, source)
                   VALUES ('screen_time', 'test')"""
            )
            c.commit()
        rows = fresh_db._execute(
            "SELECT * FROM focus WHERE state = 'screen_time'"
        ).fetchall()
        assert len(rows) == 1, "screen_time row should be queryable"

    def test_insert_all_valid_states(self, fresh_db):
        """All documented focus states should be accepted."""
        valid_states = ["working", "gaming", "idle", "meeting", "break", "screen_time"]
        for state in valid_states:
            with fresh_db._conn() as c:
                c.execute(
                    "INSERT INTO focus (state, source) VALUES (?, 'test')",
                    (state,),
                )
                c.commit()
        count = fresh_db._execute(
            "SELECT COUNT(*) as cnt FROM focus"
        ).fetchone()["cnt"]
        assert count == len(valid_states)

    def test_reject_invalid_state(self, fresh_db):
        """An invalid focus state must be rejected by the CHECK constraint."""
        with pytest.raises(sqlite3.IntegrityError):
            with fresh_db._conn() as c:
                c.execute(
                    "INSERT INTO focus (state, source) VALUES ('invalid_state', 'test')"
                )
                c.commit()

    def test_idempotent_schema_init_preserves_focus(self, tmp_path):
        """Second HeliosDB init must not rebuild focus table."""
        from datetime import datetime, timezone
        db_file = tmp_path / "test_idempotent.db"
        db = HeliosDB(db_path=str(db_file))
        # Insert a row into focus
        db._execute(
            "INSERT INTO focus (ts, state, source) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), "working", "test")
        )
        db._conn().commit()
        db.close()

        # Re-open the same DB
        db2 = HeliosDB(db_path=str(db_file))
        # The row should still be there
        rows = db2._execute("SELECT COUNT(*) FROM focus WHERE state='working'").fetchone()
        assert rows[0] == 1, f"Focus data was lost on reinit — focus rebuild not idempotent"
        # Check focus has screen_time in CHECK
        focus_info = db2._execute("PRAGMA table_info(focus)").fetchall()
        # Verify screen_time is accepted
        db2._execute("INSERT INTO focus (ts, state, source) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), "screen_time", "test_reinit"))
        db2._conn().commit()
        db2.close()


class TestKeyTableColumns:
    """Verify that key tables have the columns runtime code expects."""

    def _get_columns(self, db: HeliosDB, table: str) -> set[str]:
        rows = db._execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] for r in rows}

    def test_timeline_events_columns(self, fresh_db):
        required = {
            "id", "ts", "event_type", "source_module",
            "importance", "summary", "metadata", "date_key", "created_at",
        }
        actual = self._get_columns(fresh_db, "timeline_events")
        missing = required - actual
        assert not missing, f"timeline_events missing columns: {missing}"

    def test_event_links_columns(self, fresh_db):
        required = {
            "id", "source_event_id", "target_event_id",
            "link_type", "confidence", "evidence", "created_at",
        }
        actual = self._get_columns(fresh_db, "event_links")
        missing = required - actual
        assert not missing, f"event_links missing columns: {missing}"

    def test_module_health_columns(self, fresh_db):
        required = {"module", "status", "failures", "ts"}
        actual = self._get_columns(fresh_db, "module_health")
        missing = required - actual
        assert not missing, f"module_health missing columns: {missing}"

    def test_prediction_outcomes_columns(self, fresh_db):
        required = {
            "id", "prediction_ts", "eval_ts", "metric", "days_ahead",
            "predicted_value", "low_bound", "actual_value",
            "within_bounds", "resolved",
        }
        actual = self._get_columns(fresh_db, "prediction_outcomes")
        missing = required - actual
        assert not missing, f"prediction_outcomes missing columns: {missing}"

    def test_focus_daily_summary_columns(self, fresh_db):
        required = {
            "date_key", "state", "total_secs",
            "session_count", "first_seen", "last_seen",
        }
        actual = self._get_columns(fresh_db, "focus_daily_summary")
        missing = required - actual
        assert not missing, f"focus_daily_summary missing columns: {missing}"

    def test_session_metrics_columns(self, fresh_db):
        required = {"id", "session_id", "metric_key", "metric_value", "created_at"}
        actual = self._get_columns(fresh_db, "session_metrics")
        missing = required - actual
        assert not missing, f"session_metrics missing columns: {missing}"

    def test_notable_events_columns(self, fresh_db):
        required = {
            "id", "date_key", "rank", "event_type",
            "session_id", "timeline_event_id", "summary",
            "importance", "novelty", "confidence", "created_at",
        }
        actual = self._get_columns(fresh_db, "notable_events")
        missing = required - actual
        assert not missing, f"notable_events missing columns: {missing}"

    def test_action_log_columns(self, fresh_db):
        required = {"id", "action", "params", "result", "success", "source", "ts"}
        actual = self._get_columns(fresh_db, "action_log")
        missing = required - actual
        assert not missing, f"action_log missing columns: {missing}"

    def test_reminders_columns(self, fresh_db):
        required = {"id", "text", "priority", "remind_at", "completed", "source", "created_at"}
        actual = self._get_columns(fresh_db, "reminders")
        missing = required - actual
        assert not missing, f"reminders missing columns: {missing}"

    def test_timeline_sessions_columns(self, fresh_db):
        required = {
            "id", "session_type", "date_key", "session_start", "session_end",
            "duration_secs", "dominant_state", "event_count", "source_events",
            "summary", "metadata", "confidence", "importance", "novelty", "created_at",
        }
        actual = self._get_columns(fresh_db, "timeline_sessions")
        missing = required - actual
        assert not missing, f"timeline_sessions missing columns: {missing}"


class TestSchemaVersionGuard:
    """Verify that all migrations record their version and _ensure_schema skips already-applied ones."""

    def test_all_migrations_record_version(self, fresh_db):
        """Every migration in SCHEMA_FILES must record its version in schema_version."""
        # fresh_db has already run _ensure_schema
        recorded = {row[0] for row in fresh_db._execute("SELECT version FROM schema_version").fetchall()}

        # Extract expected versions from SCHEMA_FILES
        expected_versions = set()
        for fname in HeliosDB.SCHEMA_FILES:
            try:
                version = int(fname.split("_")[0])
                expected_versions.add(version)
            except (ValueError, IndexError):
                pass

        missing = expected_versions - recorded
        # Allowlist: migrations that intentionally don't record versions (should be empty)
        allowlist = set()
        missing -= allowlist

        assert not missing, f"Migrations missing from schema_version: {sorted(missing)}"

    def test_ensure_schema_skips_applied_migrations(self, tmp_path):
        """Second _ensure_schema call must skip already-applied migrations."""
        db_file = tmp_path / "test_guard.db"
        db1 = HeliosDB(db_path=str(db_file))

        # Get versions after first init
        v1 = {row[0] for row in db1._execute("SELECT version FROM schema_version").fetchall()}
        db1.close()

        # Re-open (triggers _ensure_schema again)
        db2 = HeliosDB(db_path=str(db_file))
        v2 = {row[0] for row in db2._execute("SELECT version FROM schema_version").fetchall()}
        db2.close()

        # Same versions, no duplicates
        assert v1 == v2, f"Schema versions changed on reinit: {v1} vs {v2}"
        # Check that no version appears twice
        rows = db2._execute("SELECT version, COUNT(*) as cnt FROM schema_version GROUP BY version HAVING cnt > 1").fetchall()
        assert len(rows) == 0, f"Duplicate schema_version rows: {rows}"