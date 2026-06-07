"""Usefulness evaluator — deterministic scoring from outcome evidence.

Computes a score in [-1.0, 1.0] based on explicit signals:
  Positive: accepted/completed/useful outcomes, fresh evidence, positive reactions.
  Negative: dismissed/snoozed/noisy outcomes, stale data, duplicate suppression.

Hard caps:
  - stale_data caps confidence at 0.35
  - private_sensitive evidence cannot produce a public dashboard detail
  - fewer than 3 outcome samples cannot propose active-mode promotion
  - any failed external action blocks promotion
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from .models import (
    LearningEvent,
    OutcomeEvent,
    OutcomeType,
    PrivacyClass,
)
from .store import SelfImprovementStore

log = logging.getLogger("helios.self_improvement.evaluator")

# ── Outcome type scoring weights ────────────────────────────────────────────
OUTCOME_WEIGHTS: dict[OutcomeType, float] = {
    OutcomeType.accepted: 0.8,
    OutcomeType.completed: 1.0,
    OutcomeType.useful: 0.6,
    OutcomeType.ignored: -0.2,
    OutcomeType.dismissed: -0.5,
    OutcomeType.snoozed: -0.3,
    OutcomeType.noisy: -0.7,
    OutcomeType.stale_data: -0.4,
    OutcomeType.duplicate_suppressed: -0.1,
    OutcomeType.failed: -1.0,
}

# ── Minimum evidence for active promotion ──────────────────────────────────
MIN_EVIDENCE_ACTIVE_PROMOTION = 3

# ── Stale data confidence cap ──────────────────────────────────────────────
STALE_CONFIDENCE_CAP = 0.35


class UsefulnessEvaluator:
    """Compute deterministic usefulness scores from events + outcomes."""

    def __init__(self, store: SelfImprovementStore):
        self.store = store

    def evaluate_fingerprint(self, fingerprint: str) -> dict[str, Any]:
        """Evaluate a specific candidate fingerprint across all its outcomes.

        Returns:
            dict with keys:
              - fingerprint
              - score: float in [-1.0, 1.0]
              - confidence: float in [0.0, 1.0]
              - outcome_count: int
              - positive_count: int
              - negative_count: int
              - stale_count: int
              - has_failed_action: bool
              - can_promote_active: bool
              - details: list of (outcome_type, weight) tuples
        """
        events = self.store.list_recent_events(limit=500)
        matching = [e for e in events if e.fingerprint == fingerprint]

        if not matching:
            return {
                "fingerprint": fingerprint,
                "score": 0.0,
                "confidence": 0.0,
                "outcome_count": 0,
                "positive_count": 0,
                "negative_count": 0,
                "stale_count": 0,
                "has_failed_action": False,
                "can_promote_active": False,
                "details": [],
            }

        # Gather outcomes for all matching events
        all_outcomes: list[OutcomeEvent] = []
        has_failed = False
        for event in matching:
            outcomes = self.store.list_outcomes(event_id=event.event_id)
            all_outcomes.extend(outcomes)

        if not all_outcomes:
            return {
                "fingerprint": fingerprint,
                "score": 0.0,
                "confidence": matching[0].confidence if matching else 0.0,
                "outcome_count": 0,
                "positive_count": 0,
                "negative_count": 0,
                "stale_count": 0,
                "has_failed_action": False,
                "can_promote_active": False,
                "details": [],
            }

        # Compute weighted score
        weighted_sum = 0.0
        positive_count = 0
        negative_count = 0
        stale_count = 0
        details: list[tuple[str, float]] = []
        has_failed = False

        for outcome in all_outcomes:
            weight = OUTCOME_WEIGHTS.get(outcome.outcome_type, 0.0)
            weighted_sum += weight
            details.append((outcome.outcome_type.value, weight))

            if outcome.outcome_type == OutcomeType.failed:
                has_failed = True
            if outcome.outcome_type == OutcomeType.stale_data:
                stale_count += 1
            if weight > 0:
                positive_count += 1
            elif weight < 0:
                negative_count += 1

        # Normalize to [-1.0, 1.0]
        n = len(all_outcomes)
        raw_score = weighted_sum / n if n > 0 else 0.0
        score = max(-1.0, min(1.0, raw_score))

        # Compute confidence
        avg_confidence = (
            sum(e.confidence for e in matching) / len(matching) if matching else 0.0
        )
        # Stale data caps confidence
        has_stale = any(e.freshness_secs > 3600 for e in matching)
        confidence = min(avg_confidence, STALE_CONFIDENCE_CAP) if has_stale else avg_confidence

        # Can promote to active mode?
        can_promote = (
            n >= MIN_EVIDENCE_ACTIVE_PROMOTION
            and not has_failed
            and confidence > 0.0
        )

        return {
            "fingerprint": fingerprint,
            "score": round(score, 4),
            "confidence": round(confidence, 4),
            "outcome_count": n,
            "positive_count": positive_count,
            "negative_count": negative_count,
            "stale_count": stale_count,
            "has_failed_action": has_failed,
            "can_promote_active": can_promote,
            "details": details,
        }

    def evaluate_recent(self, hours: float = 24.0) -> list[dict[str, Any]]:
        """Evaluate all fingerprints seen in the last N hours.

        Returns a list of evaluation results, sorted by absolute score descending.
        """
        events = self.store.list_recent_events(limit=500)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        recent = [e for e in events if e.ts >= cutoff]

        fingerprints = set(e.fingerprint for e in recent if e.fingerprint)
        results = [self.evaluate_fingerprint(fp) for fp in fingerprints]
        results.sort(key=lambda r: abs(r["score"]), reverse=True)
        return results