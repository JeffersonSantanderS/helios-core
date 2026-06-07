"""Self-Improvement Store — SQLite persistence for learning events, outcomes, proposals.

Uses its own DB file (default: ~/.hermes/helios/data/self_improvement.db) to
keep learning data separate from the main Helios state DB and avoid coupling.

All writes are idempotent. Tables are created on first connection and survive
connection reopen. Secret/private_sensitive payloads are rejected from stable
export but still stored in the DB for internal evaluation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .models import (
    LearningEvent,
    OutcomeEvent,
    OutcomeType,
    PrivacyClass,
    PolicyProposal,
    ProposalTarget,
    ProposalStatus,
    PromotionDecision,
)

log = logging.getLogger("helios.self_improvement.store")

DEFAULT_DB_PATH = Path.home() / ".hermes" / "helios" / "data" / "self_improvement.db"

# ── Dedup window: reject events with the same fingerprint within this period ─
FINGERPRINT_DEDUP_WINDOW_SECS = 300  # 5 minutes


class SelfImprovementStore:
    """SQLite-backed store for the self-improvement loop."""

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = str(DEFAULT_DB_PATH)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._ensure_tables()

    # ── Connection management ───────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """Lazy connection that reopens after close."""
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── Table creation (idempotent) ─────────────────────────────────────────

    def _ensure_tables(self) -> None:
        c = self.conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS learning_events (
                event_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                candidate_type TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.0,
                freshness_secs REAL NOT NULL DEFAULT 0.0,
                privacy_class TEXT NOT NULL DEFAULT 'public_safe',
                score REAL NOT NULL DEFAULT 0.0,
                route_decision TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_le_ts
            ON learning_events(ts)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_le_fingerprint_ts
            ON learning_events(fingerprint, ts)
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS outcome_events (
                outcome_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                outcome_type TEXT NOT NULL,
                value REAL NOT NULL DEFAULT 0.0,
                reason TEXT NOT NULL DEFAULT '',
                observed_after_secs REAL NOT NULL DEFAULT 0.0,
                FOREIGN KEY (event_id) REFERENCES learning_events(event_id)
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_oe_event_id
            ON outcome_events(event_id)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_oe_ts
            ON outcome_events(ts)
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS policy_proposals (
                proposal_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                target TEXT NOT NULL,
                change_type TEXT NOT NULL DEFAULT 'adjust',
                before_value TEXT NOT NULL DEFAULT '',
                after_value TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                evidence_count INTEGER NOT NULL DEFAULT 0,
                expected_effect TEXT NOT NULL DEFAULT '',
                risk_level TEXT NOT NULL DEFAULT 'low',
                status TEXT NOT NULL DEFAULT 'shadow',
                target_key TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_pp_status
            ON policy_proposals(status)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_pp_ts
            ON policy_proposals(ts)
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS promotion_decisions (
                decision_id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                safety_checks TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (proposal_id) REFERENCES policy_proposals(proposal_id)
            )
        """)
        self.conn.commit()

    # ── Validation ──────────────────────────────────────────────────────────

    @staticmethod
    def _validate_outcome_type(ot: str | OutcomeType) -> OutcomeType:
        if isinstance(ot, OutcomeType):
            return ot
        try:
            return OutcomeType(ot)
        except ValueError:
            valid = ", ".join(t.value for t in OutcomeType)
            raise ValueError(
                f"Invalid outcome_type '{ot}'. Valid types: {valid}"
            )

    @staticmethod
    def _validate_privacy_class(pc: str | PrivacyClass) -> PrivacyClass:
        if isinstance(pc, PrivacyClass):
            return pc
        try:
            return PrivacyClass(pc)
        except ValueError:
            valid = ", ".join(c.value for c in PrivacyClass)
            raise ValueError(
                f"Invalid privacy_class '{pc}'. Valid classes: {valid}"
            )

    # ── Record operations ───────────────────────────────────────────────────

    def record_learning_event(self, event: LearningEvent) -> str:
        """Record a learning event. Deduplicates by fingerprint within a time window.

        Returns the event_id (existing if deduped, new if inserted).
        """
        # Validate privacy class
        pc = self._validate_privacy_class(event.privacy_class)

        # Dedup check: same fingerprint within FINGERPRINT_DEDUP_WINDOW_SECS
        if event.fingerprint:
            existing = self.conn.execute(
                """SELECT event_id FROM learning_events
                   WHERE fingerprint = ? AND ts > ?
                   ORDER BY ts DESC LIMIT 1""",
                (event.fingerprint,
                 _ts_minus_seconds(event.ts, FINGERPRINT_DEDUP_WINDOW_SECS)),
            ).fetchone()
            if existing:
                log.debug("Deduped learning event fingerprint=%s → %s",
                          event.fingerprint, existing["event_id"])
                return existing["event_id"]

        self.conn.execute(
            """INSERT INTO learning_events
               (event_id, ts, source, candidate_type, fingerprint, evidence,
                confidence, freshness_secs, privacy_class, score, route_decision)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.event_id, event.ts, event.source, event.candidate_type,
             event.fingerprint, event.evidence, event.confidence,
             event.freshness_secs, pc.value, event.score, event.route_decision),
        )
        self.conn.commit()
        return event.event_id

    def record_outcome(self, outcome: OutcomeEvent) -> str:
        """Record an outcome event linked to a learning event."""
        ot = self._validate_outcome_type(outcome.outcome_type)
        self.conn.execute(
            """INSERT INTO outcome_events
               (outcome_id, event_id, ts, outcome_type, value, reason,
                observed_after_secs)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (outcome.outcome_id, outcome.event_id, outcome.ts, ot.value,
             outcome.value, outcome.reason, outcome.observed_after_secs),
        )
        self.conn.commit()
        return outcome.outcome_id

    def list_recent_events(self, limit: int = 100) -> list[LearningEvent]:
        """List recent learning events, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM learning_events ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_learning_event(r) for r in rows]

    def list_outcomes(
        self,
        event_id: str | None = None,
        limit: int = 100,
    ) -> list[OutcomeEvent]:
        """List outcome events, optionally filtered by event_id."""
        if event_id:
            rows = self.conn.execute(
                """SELECT * FROM outcome_events
                   WHERE event_id = ? ORDER BY ts DESC LIMIT ?""",
                (event_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM outcome_events ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_outcome_event(r) for r in rows]

    def upsert_policy_proposal(self, proposal: PolicyProposal) -> str:
        """Insert or update a policy proposal. Returns the proposal_id."""
        self.conn.execute(
            """INSERT INTO policy_proposals
               (proposal_id, ts, target, change_type, before_value,
                after_value, reason, evidence_count, expected_effect,
                risk_level, status, target_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(proposal_id) DO UPDATE SET
                   ts = excluded.ts,
                   target = excluded.target,
                   change_type = excluded.change_type,
                   before_value = excluded.before_value,
                   after_value = excluded.after_value,
                   reason = excluded.reason,
                   evidence_count = excluded.evidence_count,
                   expected_effect = excluded.expected_effect,
                   risk_level = excluded.risk_level,
                   status = excluded.status,
                   target_key = excluded.target_key""",
            (proposal.proposal_id, proposal.ts, proposal.target.value,
             proposal.change_type, proposal.before, proposal.after,
             proposal.reason, proposal.evidence_count, proposal.expected_effect,
             proposal.risk_level, proposal.status.value, proposal.target_key),
        )
        self.conn.commit()
        return proposal.proposal_id

    def record_promotion_decision(self, decision: PromotionDecision) -> str:
        """Record whether a policy proposal was approved or blocked."""
        self.conn.execute(
            """INSERT INTO promotion_decisions
               (decision_id, proposal_id, ts, decision, reason, safety_checks)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (decision.decision_id, decision.proposal_id, decision.ts,
             decision.decision, decision.reason, decision.safety_checks),
        )
        self.conn.commit()
        return decision.decision_id

    def get_proposal(self, proposal_id: str) -> PolicyProposal | None:
        """Fetch a single proposal by ID."""
        row = self.conn.execute(
            "SELECT * FROM policy_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_proposal(row)

    def list_proposals(
        self,
        status: str | ProposalStatus | None = None,
        limit: int = 100,
    ) -> list[PolicyProposal]:
        """List proposals, optionally filtered by status."""
        if status:
            s = status.value if isinstance(status, ProposalStatus) else status
            rows = self.conn.execute(
                """SELECT * FROM policy_proposals
                   WHERE status = ? ORDER BY ts DESC LIMIT ?""",
                (s, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM policy_proposals ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_proposal(r) for r in rows]

    def count_events_24h(self) -> int:
        """Count learning events in the last 24 hours."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM learning_events WHERE ts > ?",
            (cutoff,),
        ).fetchone()
        return row["cnt"] if row else 0

    def count_outcomes_24h(self) -> int:
        """Count outcome events in the last 24 hours."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM outcome_events WHERE ts > ?",
            (cutoff,),
        ).fetchone()
        return row["cnt"] if row else 0

    def count_proposals_by_status(self) -> dict[str, int]:
        """Count proposals grouped by status."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM policy_proposals GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}


# ── Row → dataclass converters ──────────────────────────────────────────────

def _row_to_learning_event(row: sqlite3.Row) -> LearningEvent:
    return LearningEvent(
        event_id=row["event_id"],
        ts=row["ts"],
        source=row["source"],
        candidate_type=row["candidate_type"],
        fingerprint=row["fingerprint"],
        evidence=row["evidence"],
        confidence=row["confidence"],
        freshness_secs=row["freshness_secs"],
        privacy_class=PrivacyClass(row["privacy_class"]),
        score=row["score"],
        route_decision=row["route_decision"],
    )


def _row_to_outcome_event(row: sqlite3.Row) -> OutcomeEvent:
    return OutcomeEvent(
        outcome_id=row["outcome_id"],
        event_id=row["event_id"],
        ts=row["ts"],
        outcome_type=OutcomeType(row["outcome_type"]),
        value=row["value"],
        reason=row["reason"],
        observed_after_secs=row["observed_after_secs"],
    )


def _row_to_proposal(row: sqlite3.Row) -> PolicyProposal:
    return PolicyProposal(
        proposal_id=row["proposal_id"],
        ts=row["ts"],
        target=ProposalTarget(row["target"]) if row["target"] else ProposalTarget.priority_weight,
        change_type=row["change_type"],
        before=row["before_value"],
        after=row["after_value"],
        reason=row["reason"],
        evidence_count=row["evidence_count"],
        expected_effect=row["expected_effect"],
        risk_level=row["risk_level"],
        status=ProposalStatus(row["status"]) if row["status"] else ProposalStatus.shadow,
        target_key=row["target_key"],
    )


def _ts_minus_seconds(ts: str, seconds: float) -> str:
    """Subtract seconds from an ISO timestamp string."""
    try:
        dt = datetime.fromisoformat(ts)
        return (dt - timedelta(seconds=seconds)).isoformat()
    except (ValueError, TypeError):
        # Fallback: return a sufficiently old timestamp
        return (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()