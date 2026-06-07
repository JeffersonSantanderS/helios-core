"""Helios v6 — Unified SQLite state layer.

Replaces v4 JSON-only state with a shared SQLite Brain.
Both script engine and LLM bridge read/write through this layer.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("helios.state")


class HeliosDB:
    """Thread-safe SQLite state access."""

    SCHEMA_FILES = [
        "001_schema_v5.sql",
        "002_calendar_events.sql",
        "003_briefing_log.sql",
        "004_correlations.sql",
        "005_nl_queries.sql",
        "006_subscriptions.sql",
        "007_goals.sql",
        "008_seed_rules.sql",
        "009_proactive_rules.sql",
        "010_v6_rules.sql",
        "011_v6_alert_schema.sql",
        "018_timeline_events.sql",
        "019_timeline_sessions.sql",
        "020_disable_superseded_rules.sql",
        "021_priority_engine.sql",
        "022_candidate_fingerprint.sql",
        "023_nutrition_rules.sql",
        "024_scheduler_jobs.sql",
        "025_delivery_ledger.sql",
        "026_v6_schema_gaps.sql",
        "027_focus_screen_time.sql",
    ]

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            base = os.environ.get(
                "HELIOS_BASE",
                os.path.join(os.path.expanduser("~"), ".hermes", "helios"),
            )
            db_path = os.path.join(base, "helios_v6.db")
        self.db_path = db_path
        self._local = threading.local()
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        """Get thread-local connection, recreating if closed."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
            return self._local.conn
        # Detect closed connections and recreate
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _execute(self, sql: str, params: tuple = ()):
        """Execute a parameterized SQL statement and return the cursor."""
        conn = self._conn()
        return conn.execute(sql, params)

    def _ensure_schema(self):
        migrations_dir = Path(__file__).parent / "migrations"
        with self._conn() as c:
            # Get already-applied migration versions
            applied = set()
            try:
                rows = c.execute("SELECT version FROM schema_version").fetchall()
                applied = {row[0] for row in rows}
            except Exception:
                pass  # schema_version table might not exist yet

            for fname in self.SCHEMA_FILES:
                # Extract version number from filename (e.g., "026_v6_schema_gaps.sql" -> 26)
                version = None
                try:
                    version = int(fname.split("_")[0])
                except (ValueError, IndexError):
                    pass

                # Skip if this migration version was already applied
                if version is not None and version in applied:
                    continue

                fpath = migrations_dir / fname
                if fpath.exists():
                    try:
                        c.executescript(fpath.read_text())
                    except sqlite3.OperationalError as exc:
                        # Idempotent: ignore "duplicate column", "table already exists", etc.
                        msg = str(exc).lower()
                        if "duplicate" in msg or "already exists" in msg:
                            continue
                        raise
            c.commit()

    # ── context ───────────────────────────────────────────────────

    def set_context(
        self,
        source: str,
        module: str,
        key: str,
        value: Any,
        priority: int = 0,
        expires_at: Optional[str] = None,
    ) -> None:
        """Upsert a context row."""
        with self._conn() as c:
            c.execute(
                """INSERT INTO context (source, module, key, value, priority, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(module, key, source) DO UPDATE SET
                     value=excluded.value,
                     priority=excluded.priority,
                     ts=excluded.ts,
                     expires_at=excluded.expires_at""",
                (source, module, key, json.dumps(value), priority, expires_at),
            )

    def get_context_since(self, hours: int = 24) -> list[dict]:
        """Return context entries from the last N hours."""
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return [
            dict(r) for r in self._execute(
                "SELECT * FROM context WHERE ts >= ? ORDER BY ts DESC",
                (since,),
            ).fetchall()
        ]

    def delete_context_before(self, cutoff_ts: str) -> int:
        """Delete context entries older than cutoff_ts. Returns count deleted."""
        cur = self._execute(
            "DELETE FROM context WHERE ts < ?",
            (cutoff_ts,),
        )
        self._conn().commit()
        return cur.rowcount

    def get_decisions_by_type(self, decision_type: str, limit: int = 1) -> list[dict]:
        """Return decisions of a specific type, newest first."""
        return [
            dict(r) for r in self._execute(
                "SELECT * FROM decisions WHERE decision_type = ? ORDER BY ts DESC LIMIT ?",
                (decision_type, limit),
            ).fetchall()
        ]

    def get_latest_context(self, module: Optional[str] = None) -> Optional[dict]:
        """Return the most recent context entry, optionally filtered by module."""
        if module:
            rows = self._execute(
                "SELECT * FROM context WHERE module = ? ORDER BY ts DESC LIMIT 1",
                (module,),
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT * FROM context ORDER BY ts DESC LIMIT 1",
            ).fetchall()
        return dict(rows[0]) if rows else None

    def get_context(self, module: Optional[str] = None, key: Optional[str] = None) -> list[dict]:
        """Query context rows."""
        sql = "SELECT * FROM context WHERE 1=1"
        params: list = []
        if module:
            sql += " AND module=?"
            params.append(module)
        if key:
            sql += " AND key=?"
            params.append(key)
        sql += " ORDER BY priority DESC, ts DESC"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_context_value(self, module: str, key: str) -> Any:
        rows = self.get_context(module, key)
        if not rows:
            return None
        try:
            return json.loads(rows[0]["value"])
        except json.JSONDecodeError:
            return rows[0]["value"]

    # ── rules ─────────────────────────────────────────────────────

    def get_rules(self, enabled: bool = True) -> list[dict]:
        sql = "SELECT * FROM rules WHERE enabled=1" if enabled else "SELECT * FROM rules"
        with self._conn() as c:
            rows = c.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def get_rule(self, slug: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM rules WHERE slug=?", (slug,)).fetchone()
        return dict(row) if row else None

    # ── decisions ─────────────────────────────────────────────────

    def insert_decision(
        self,
        decision_type: str,
        source: str,
        action: str,
        context: Optional[dict] = None,
        outcome: Optional[str] = None,
        module: Optional[str] = None,
        rule_id: Optional[str] = None,
    ) -> int:
        with self._conn() as c:
            c.execute(
                """INSERT INTO decisions
                   (decision_type, source, context, action, outcome, module, rule_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_type,
                    source,
                    json.dumps(context or {}),
                    action,
                    outcome,
                    module,
                    rule_id,
                ),
            )
            c.commit()
            return 0

    # ── llm_requests ───────────────────────────────────────────────

    def queue_llm_request(
        self,
        request_type: str,
        context_keys: list[str],
        prompt_template: Optional[str] = None,
        max_tokens: int = 512,
        priority: int = 1,
    ) -> int:
        with self._conn() as c:
            c.execute(
                """INSERT INTO llm_requests
                   (request_type, context_keys, prompt_template, max_tokens, priority)
                   VALUES (?, ?, ?, ?, ?)""",
                (request_type, json.dumps(context_keys), prompt_template, max_tokens, priority),
            )
            c.commit()
            return 0

    def get_pending_llm_requests(self, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM llm_requests
                   WHERE status = 'pending'
                   ORDER BY priority DESC, ts
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_llm_request(
        self,
        req_id: int,
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
        model_used: Optional[str] = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE llm_requests
                   SET status=?, result=?, result_ts=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                   error=?, model_used=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                   WHERE id=?""",
                (status, result, error, model_used, req_id),
            )
            c.commit()

    # ── module health / circuit breaker ───────────────────────────

    def get_module_health(self, module: str) -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM module_health WHERE module=? ORDER BY ts DESC LIMIT 1",
                (module,),
            ).fetchone()
        if row:
            return dict(row)
        return {"module": module, "status": "unknown", "failures": 0}

    def set_module_health(self, module: str, status: str, failures: int = 0) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO module_health (module, status, failures)
                   VALUES (?, ?, ?)""",
                (module, status, failures),
            )
            c.commit()

    # ── convenience ───────────────────────────────────────────────

    def today_llm_call_count(self) -> int:
        with self._conn() as c:
            row = c.execute(
                """SELECT COUNT(*) FROM llm_requests
                   WHERE date(ts)=date('now') AND status='done'"""
            ).fetchone()
        return row[0] if row else 0

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

