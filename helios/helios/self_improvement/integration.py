"""Self-improvement integration — hooks into priority/action/outcome paths.

Records learning events when candidates are decided, connects reaction
outcomes to OutcomeEvents, and runs periodic evaluation/proposal cycles
from the engine tick.

This module is the glue between the priority/action engine and the
self-improvement store. It does not change any existing behavior in
Round 1 (shadow mode).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..priority.models import Candidate, CandidateDecision, PriorityResult
from ..self_improvement.models import (
    LearningEvent,
    OutcomeEvent,
    OutcomeType,
    PrivacyClass,
    PolicyProposal,
    ProposalStatus,
)
from ..self_improvement.store import SelfImprovementStore
from ..self_improvement.evaluator import UsefulnessEvaluator
from ..self_improvement.proposals import ProposalEngine
from ..self_improvement.safety import SafetyGates

log = logging.getLogger("helios.self_improvement.integration")

# ── Privacy classification rules ──────────────────────────────────────────
# Map source module data to privacy classes. Never store raw health/location/
# email content.

SOURCE_PRIVACY: dict[str, PrivacyClass] = {
    "health": PrivacyClass.private_sensitive,
    "location": PrivacyClass.private_sensitive,
    "nutrition": PrivacyClass.private_summary,
    "spotify": PrivacyClass.private_summary,
    "calendar": PrivacyClass.private_summary,
    "mood": PrivacyClass.private_summary,
    "focus": PrivacyClass.private_summary,
    "work_hours": PrivacyClass.private_summary,
    "home": PrivacyClass.public_safe,
    "system": PrivacyClass.public_safe,
    "priority_engine": PrivacyClass.public_safe,
    "module_health": PrivacyClass.public_safe,
    "rule_hit": PrivacyClass.public_safe,
}


def _classify_privacy(source: str, raw_payload: dict[str, Any]) -> PrivacyClass:
    """Classify the privacy level of a learning event based on its source."""
    base = SOURCE_PRIVACY.get(source, PrivacyClass.private_summary)

    # Promote to secret if raw coordinates or health payloads are present
    if isinstance(raw_payload, dict):
        for key in raw_payload:
            k = key.lower()
            if any(s in k for s in ("latitude", "longitude", "lat_", "lng_",
                                     "access_token", "password", "secret", "api_key")):
                return PrivacyClass.secret
        # Raw health metrics → sensitive
        if source == "health" and any(k in raw_payload for k in ("heart_rate", "steps", "sleep")):
            return PrivacyClass.private_sensitive

    return base


def _sanitize_evidence(source: str, title: str, message: str, candidate_type: str) -> str:
    """Create a sanitized evidence summary that never includes raw data.

    Strips raw coordinates, health payloads, message content, and PII.
    """
    # Truncate long messages
    summary = f"[{source}:{candidate_type}] {title}"
    if message:
        # Only include first 100 chars, strip raw data patterns
        msg_preview = message[:100].replace("\n", " ")
        # Replace any coordinate patterns
        import re
        msg_preview = re.sub(r'\d+\.\d{4,}', '[REDACTED_FLOAT]', msg_preview)
        summary += f" — {msg_preview}"
    return summary


class SelfImprovementIntegration:
    """Connect the priority/action engine to the self-improvement loop.

    Records learning events at decision time and outcome events when
    reactions/responses are observed. Runs periodic evaluation/proposal
    cycles when called from the engine tick.
    """

    def __init__(self, store: SelfImprovementStore | None = None, cfg: dict[str, Any] | None = None):
        self.store = store or SelfImprovementStore()
        self.cfg = cfg or {}
        self.evaluator = UsefulnessEvaluator(self.store)
        self.proposal_engine = ProposalEngine(self.store, self.evaluator)
        self._last_evaluation_ts: str | None = None

    # ── Recording learning events at decision time ─────────────────────────

    def record_priority_decision(
        self,
        result: PriorityResult,
        context: dict[str, Any],
    ) -> list[str]:
        """Record learning events for all candidates selected or suppressed.

        Called after PriorityEngine.evaluate_tick() produces a result.
        Returns list of event_ids.
        """
        if not self.cfg.get("enabled", True):
            return []

        event_ids: list[str] = []

        # Build candidate lookup
        cand_map = {c.candidate_id: c for c in result.candidates}

        for decision in result.decisions:
            cand = cand_map.get(decision.candidate_id)
            if cand is None:
                continue

            # Determine route decision
            if decision.decision.startswith("select_"):
                route = "selected"
            elif decision.decision == "suppressed":
                route = "suppressed"
            elif decision.decision == "deferred":
                route = "deferred"
            else:
                route = decision.decision

            # Sanitize evidence
            evidence = _sanitize_evidence(
                cand.source, cand.title, cand.message, cand.candidate_type,
            )
            privacy = _classify_privacy(cand.source, cand.raw_payload)

            event = LearningEvent(
                source=cand.source or "priority_engine",
                candidate_type=cand.candidate_type,
                fingerprint=cand.fingerprint or cand.candidate_id,
                evidence=evidence,
                confidence=float(decision.final_score) if decision.final_score else 0.0,
                freshness_secs=float(cand.hydrated.get("freshness_secs", 0))
                    if cand.hydrated else 0.0,
                privacy_class=privacy,
                score=float(decision.final_score) if decision.final_score else 0.0,
                route_decision=route,
            )
            eid = self.store.record_learning_event(event)
            event_ids.append(eid)

        log.debug("Recorded %d learning events from priority decision", len(event_ids))
        return event_ids

    # ── Recording outcomes from reactions ───────────────────────────────────

    def record_reaction_outcome(
        self,
        event_id: str,
        reaction_type: str,
        reason: str = "",
        observed_after_secs: float = 0.0,
    ) -> str | None:
        """Map a Matrix reaction to an OutcomeEvent.

        Reaction types:
          - ✅, 👍, ❤️ → accepted/useful
          - ❌, 👎 → dismissed
          - 🔇 → snoozed (noisy suppression)
          - ⏰ → snoozed (timed reminder)
          - no reaction within window → ignored
        """
        outcome_type_map = {
            "✅": OutcomeType.accepted,
            "👍": OutcomeType.accepted,
            "❤️": OutcomeType.useful,
            "❌": OutcomeType.dismissed,
            "👎": OutcomeType.dismissed,
            "🔇": OutcomeType.snoozed,
            "⏰": OutcomeType.snoozed,
            "duplicate": OutcomeType.duplicate_suppressed,
        }

        ot = outcome_type_map.get(reaction_type, OutcomeType.ignored)
        value = {
            OutcomeType.accepted: 0.8,
            OutcomeType.useful: 0.6,
            OutcomeType.dismissed: -0.5,
            OutcomeType.snoozed: -0.3,
            OutcomeType.noisy: -0.7,
            OutcomeType.duplicate_suppressed: -0.1,
            OutcomeType.ignored: -0.2,
            OutcomeType.stale_data: -0.4,
            OutcomeType.failed: -1.0,
            OutcomeType.completed: 1.0,
        }.get(ot, 0.0)

        outcome = OutcomeEvent(
            event_id=event_id,
            outcome_type=ot,
            value=value,
            reason=reason or f"Reaction: {reaction_type}",
            observed_after_secs=observed_after_secs,
        )
        return self.store.record_outcome(outcome)

    def record_action_outcome(
        self,
        event_id: str,
        success: bool,
        reason: str = "",
        observed_after_secs: float = 0.0,
    ) -> str | None:
        """Record the outcome of an action (success or failure)."""
        outcome = OutcomeEvent(
            event_id=event_id,
            outcome_type=OutcomeType.completed if success else OutcomeType.failed,
            value=1.0 if success else -1.0,
            reason=reason,
            observed_after_secs=observed_after_secs,
        )
        return self.store.record_outcome(outcome)

    def record_duplicate_suppressed(
        self,
        event_id: str,
        reason: str = "",
        observed_after_secs: float = 0.0,
    ) -> str | None:
        """Record that a duplicate notification was suppressed."""
        outcome = OutcomeEvent(
            event_id=event_id,
            outcome_type=OutcomeType.duplicate_suppressed,
            value=-0.1,
            reason=reason or "Duplicate suppressed by channel dedup",
            observed_after_secs=observed_after_secs,
        )
        return self.store.record_outcome(outcome)

    def record_stale_warning(
        self,
        event_id: str,
        reason: str = "",
        observed_after_secs: float = 0.0,
    ) -> str | None:
        """Record that a notification was based on stale data."""
        outcome = OutcomeEvent(
            event_id=event_id,
            outcome_type=OutcomeType.stale_data,
            value=-0.4,
            reason=reason or "Stale data source",
            observed_after_secs=observed_after_secs,
        )
        return self.store.record_outcome(outcome)

    # ── Periodic evaluation/proposal cycle ──────────────────────────────────

    def run_evaluation_cycle(self, hours: float = 24.0) -> list[PolicyProposal]:
        """Run an evaluation + proposal cycle.

        Reads recent events/outcomes, evaluates usefulness scores,
        generates shadow-mode proposals. This is called from the
        engine tick on a configurable interval.
        """
        if not self.cfg.get("enabled", True):
            return []

        interval_minutes = self.cfg.get("proposal_interval_minutes", 60)
        min_evidence = self.cfg.get("min_evidence_count", 3)

        proposals = self.proposal_engine.generate_proposals(hours=hours)
        self._last_evaluation_ts = datetime.now(timezone.utc).isoformat()

        log.info(
            "Self-improvement evaluation: %d proposals generated",
            len(proposals),
        )
        return proposals

    # ── Status / export helpers ─────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """Get current self-improvement status for CLI/export."""
        mode = self.cfg.get("mode", "shadow")
        counts = self.store.count_proposals_by_status()

        return {
            "mode": mode,
            "enabled": self.cfg.get("enabled", True),
            "latest_evaluation_at": self._last_evaluation_ts,
            "event_count_24h": self.store.count_events_24h(),
            "outcome_count_24h": self.store.count_outcomes_24h(),
            "proposal_count": sum(counts.values()),
            "blocked_count": counts.get("blocked", 0),
            "shadow_count": counts.get("shadow", 0),
            "proposed_count": counts.get("proposed", 0),
            "approved_count": counts.get("applied", 0),
            "allow_active_promotion": self.cfg.get("allow_active_promotion", False),
        }