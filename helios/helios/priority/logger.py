"""Priority Engine logger — write candidates/scores/decisions to SQLite."""

from __future__ import annotations

import json
import logging
from typing import Any

from .models import Candidate, CandidateScore, CandidateDecision

log = logging.getLogger("helios.priority.logger")


class PriorityLogger:
    """Persist priority pipeline output to the DB."""

    def __init__(self, db: Any):
        self.db = db

    def log_all(
        self,
        tick_id: str,
        candidates: list[Candidate],
        scores: list[CandidateScore],
        decisions: list[CandidateDecision],
    ) -> None:
        """Log all pipeline artifacts."""
        try:
            with self.db._conn() as c:
                for cand in candidates:
                    self._insert_candidate(c, cand)
                for sc in scores:
                    self._insert_score(c, tick_id, sc)
                for dec in decisions:
                    self._insert_decision(c, tick_id, dec)
                c.commit()
        except Exception as exc:
            log.warning("PriorityLogger failed: %s", exc)

    def _insert_candidate(self, conn: Any, cand: Candidate) -> None:
        conn.execute(
            """INSERT INTO priority_candidates
               (candidate_id, tick_id, source, candidate_type, title, message,
                severity, category, priority_hint, module, rule_slug,
                action_name, action_config_json, raw_payload_json, hydrated_json,
                tags_json, status, fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(candidate_id) DO UPDATE SET
                 status=excluded.status,
                 hydrated_json=excluded.hydrated_json""",
            (
                cand.candidate_id,
                cand.tick_id,
                cand.source,
                cand.candidate_type,
                cand.title,
                cand.message,
                cand.severity,
                cand.category,
                cand.priority_hint,
                cand.module,
                cand.rule_slug,
                cand.action_name,
                json.dumps(cand.action_config),
                json.dumps(cand.raw_payload),
                json.dumps(cand.hydrated),
                json.dumps(cand.tags),
                cand.status,
                cand.fingerprint,
            ),
        )

    def _insert_score(self, conn: Any, tick_id: str, sc: CandidateScore) -> None:
        conn.execute(
            """INSERT INTO priority_scores
               (candidate_id, tick_id, urgency, importance, relevance,
                confidence, context_fit, actionability, novelty, safety,
                disruption_cost, staleness, annoyance, redundancy,
                final_score, explanation, factors_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sc.candidate_id,
                tick_id,
                sc.urgency,
                sc.importance,
                sc.relevance,
                sc.confidence,
                sc.context_fit,
                sc.actionability,
                sc.novelty,
                sc.safety,
                sc.disruption_cost,
                sc.staleness,
                sc.annoyance,
                sc.redundancy,
                sc.final_score,
                sc.explanation,
                json.dumps(sc.factors),
            ),
        )

    def _insert_decision(self, conn: Any, tick_id: str, dec: CandidateDecision) -> None:
        conn.execute(
            """INSERT INTO priority_decisions
               (candidate_id, tick_id, decision, route, reason,
                final_score, threshold_used, execute_now, mode)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dec.candidate_id,
                tick_id,
                dec.decision,
                dec.route,
                dec.reason,
                dec.final_score,
                dec.threshold_used,
                1 if dec.execute_now else 0,
                dec.mode,
            ),
        )
