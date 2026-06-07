"""Policy proposal generator — turns evaluation results into safe proposals.

Proposals are always created in shadow status. No active behavior changes
in Round 1. Proposals target specific config keys / policy adjustments and
carry evidence counts and risk levels.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .models import (
    PolicyProposal,
    ProposalTarget,
    ProposalStatus,
)
from .evaluator import UsefulnessEvaluator
from .store import SelfImprovementStore
from .safety import SafetyGates

log = logging.getLogger("helios.self_improvement.proposals")

# ── Mapping from evaluation signals to proposal targets ────────────────────
# When a fingerprint pattern scores high/low, we propose adjustments to the
# relevant config knob.

SIGNAL_TARGET_MAP: dict[str, ProposalTarget] = {
    "notification_cooldown": ProposalTarget.cooldown_secs,
    "priority_adjustment": ProposalTarget.priority_weight,
    "quiet_hours": ProposalTarget.quiet_hour_rule,
    "freshness": ProposalTarget.stale_threshold,
    "candidate_on_off": ProposalTarget.candidate_enablement,
    "dashboard_visibility": ProposalTarget.dashboard_visibility,
    "notification_template": ProposalTarget.notification_template,
}


class ProposalEngine:
    """Generate policy proposals from evaluation results."""

    def __init__(self, store: SelfImprovementStore, evaluator: UsefulnessEvaluator | None = None):
        self.store = store
        self.evaluator = evaluator or UsefulnessEvaluator(store)
        self.safety = SafetyGates(store)

    def generate_proposals(self, hours: float = 24.0) -> list[PolicyProposal]:
        """Evaluate recent fingerprints and generate shadow-mode proposals.

        Only generates proposals for fingerprints with at least 3 outcome samples
        and a non-zero score. All proposals start as status=shadow.
        Returns the list of newly created proposals.
        """
        evaluations = self.evaluator.evaluate_recent(hours=hours)
        proposals: list[PolicyProposal] = []

        for ev in evaluations:
            if ev["outcome_count"] < 3:
                continue  # Not enough evidence
            if abs(ev["score"]) < 0.1:
                continue  # Negligible signal

            # Determine proposal target from evaluation context
            target, target_key, change_type, before, after, reason = (
                self._infer_proposal(ev)
            )
            if target is None:
                continue

            proposal = PolicyProposal(
                target=target,
                change_type=change_type,
                before=before,
                after=after,
                reason=reason,
                evidence_count=ev["outcome_count"],
                expected_effect=self._expected_effect(ev, target),
                risk_level=self._risk_level(ev),
                status=ProposalStatus.shadow,  # Always shadow in Round 1
                target_key=target_key,
            )

            # Run safety checks — this sets proposal status to blocked if needed
            self.safety.check(proposal)
            pid = self.store.upsert_policy_proposal(proposal)

            fetched = self.store.get_proposal(pid)
            if fetched:
                proposals.append(fetched)

        log.info("Generated %d proposals from %d evaluations", len(proposals), len(evaluations))
        return proposals

    def _infer_proposal(
        self, ev: dict[str, Any]
    ) -> tuple[ProposalTarget | None, str, str, str, str, str]:
        """Infer what config change to propose from an evaluation result.

        Returns (target, target_key, change_type, before, after, reason).
        Returns (None, ...) if no sensible proposal can be inferred.
        """
        score = ev["score"]
        fingerprint = ev.get("fingerprint", "")

        # Positive scores → reinforce (increase weight or lower cooldown)
        # Negative scores → dampen (decrease weight or increase cooldown)
        if score > 0.3:
            # Reinforce: lower cooldown or increase weight
            return (
                ProposalTarget.cooldown_secs,
                "priority.safeguards.quiet_hours_penalty",
                "adjust",
                "0.25",
                str(round(max(0.1, 0.25 - score * 0.1), 2)),
                f"Fingerprint '{fingerprint}' scored +{score:.2f}: "
                f"consider lowering quiet hours penalty (positive reception).",
            )
        elif score < -0.3:
            # Dampen: increase cooldown
            return (
                ProposalTarget.cooldown_secs,
                "cooldown.notification_default",
                "adjust",
                "300",
                "600",
                f"Fingerprint '{fingerprint}' scored {score:.2f}: "
                f"consider increasing notification cooldown (negative reception).",
            )
        # Mild signal — adjust stale threshold
        if ev["stale_count"] > 0:
            return (
                ProposalTarget.stale_threshold,
                "freshness.stale_threshold_secs",
                "adjust",
                "600",
                "300",
                f"Fingerprint '{fingerprint}' has {ev['stale_count']} stale outcomes: "
                f"consider lowering stale threshold for more aggressive data freshness.",
            )

        return None, "", "", "", "", ""

    def _expected_effect(self, ev: dict[str, Any], target: ProposalTarget) -> str:
        """Describe expected effect of applying this proposal."""
        score = ev["score"]
        if score > 0.3:
            return f"Expected: +{abs(score)*15:.0f}% engagement on similar alerts."
        elif score < -0.3:
            return f"Expected: {abs(score)*10:.0f}% fewer dismissals on similar alerts."
        return "Marginal effect expected."

    def _risk_level(self, ev: dict[str, Any]) -> str:
        """Determine risk level for a proposal."""
        if ev["has_failed_action"]:
            return "high"
        if ev["stale_count"] > 2:
            return "medium"
        if ev["negative_count"] > ev["positive_count"]:
            return "medium"
        return "low"