"""Self-improvement data models.

Defines the core data structures for the closed-loop learning substrate:
  - LearningEvent: what Helios decided and why
  - OutcomeEvent: what happened after a decision
  - PolicyProposal: a proposed change to config/policy
  - PromotionDecision: an approval/rejection of a proposal

All models use dataclasses and serialise to dict/JSON for SQLite storage.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ── Enums ──────────────────────────────────────────────────────────────────

class OutcomeType(str, Enum):
    """How a learning event turned out for the user."""
    accepted = "accepted"
    dismissed = "dismissed"
    ignored = "ignored"
    snoozed = "snoozed"
    completed = "completed"
    failed = "failed"
    stale_data = "stale_data"
    duplicate_suppressed = "duplicate_suppressed"
    noisy = "noisy"
    useful = "useful"


class PrivacyClass(str, Enum):
    """Privacy classification for event evidence."""
    public_safe = "public_safe"
    private_summary = "private_summary"
    private_sensitive = "private_sensitive"
    secret = "secret"


class ProposalTarget(str, Enum):
    """What a policy proposal targets."""
    priority_weight = "priority_weight"
    cooldown_secs = "cooldown_secs"
    quiet_hour_rule = "quiet_hour_rule"
    stale_threshold = "stale_threshold"
    candidate_enablement = "candidate_enablement"
    dashboard_visibility = "dashboard_visibility"
    notification_template = "notification_template"


class ProposalStatus(str, Enum):
    """Lifecycle states for a policy proposal."""
    shadow = "shadow"
    proposed = "proposed"
    approved = "approved"
    blocked = "blocked"
    applied = "applied"
    reverted = "reverted"


# ── Helpers ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex[:16]


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class LearningEvent:
    """A record of what Helios decided and why."""
    event_id: str = field(default_factory=_uuid)
    ts: str = field(default_factory=_now_iso)
    source: str = ""
    candidate_type: str = ""
    fingerprint: str = ""
    evidence: str = ""          # sanitized summary only — never raw payloads
    confidence: float = 0.0    # [0.0, 1.0]
    freshness_secs: float = 0.0
    privacy_class: PrivacyClass = PrivacyClass.public_safe
    score: float = 0.0
    route_decision: str = ""    # selected / suppressed / deferred

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "ts": self.ts,
            "source": self.source,
            "candidate_type": self.candidate_type,
            "fingerprint": self.fingerprint,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "freshness_secs": self.freshness_secs,
            "privacy_class": self.privacy_class.value,
            "score": self.score,
            "route_decision": self.route_decision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LearningEvent":
        raw_pc = data.get("privacy_class", "public_safe")
        pc = PrivacyClass(raw_pc) if isinstance(raw_pc, str) else raw_pc
        return cls(
            event_id=data.get("event_id", _uuid()),
            ts=data.get("ts", _now_iso()),
            source=data.get("source", ""),
            candidate_type=data.get("candidate_type", ""),
            fingerprint=data.get("fingerprint", ""),
            evidence=data.get("evidence", ""),
            confidence=float(data.get("confidence", 0.0)),
            freshness_secs=float(data.get("freshness_secs", 0.0)),
            privacy_class=pc,
            score=float(data.get("score", 0.0)),
            route_decision=data.get("route_decision", ""),
        )


@dataclass
class OutcomeEvent:
    """What happened after a learning event — did the nudge help?"""
    outcome_id: str = field(default_factory=_uuid)
    event_id: str = ""
    ts: str = field(default_factory=_now_iso)
    outcome_type: OutcomeType = OutcomeType.ignored
    value: float = 0.0          # [−1.0, 1.0] numeric signal
    reason: str = ""
    observed_after_secs: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "event_id": self.event_id,
            "ts": self.ts,
            "outcome_type": self.outcome_type.value,
            "value": self.value,
            "reason": self.reason,
            "observed_after_secs": self.observed_after_secs,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OutcomeEvent":
        raw_ot = data.get("outcome_type", "ignored")
        ot = OutcomeType(raw_ot) if isinstance(raw_ot, str) else raw_ot
        return cls(
            outcome_id=data.get("outcome_id", _uuid()),
            event_id=data.get("event_id", ""),
            ts=data.get("ts", _now_iso()),
            outcome_type=ot,
            value=float(data.get("value", 0.0)),
            reason=data.get("reason", ""),
            observed_after_secs=float(data.get("observed_after_secs", 0.0)),
        )


@dataclass
class PolicyProposal:
    """A proposed change to config or policy, derived from outcome evidence."""
    proposal_id: str = field(default_factory=_uuid)
    ts: str = field(default_factory=_now_iso)
    target: ProposalTarget = ProposalTarget.priority_weight
    change_type: str = "adjust"       # adjust | enable | disable | threshold
    before: str = ""
    after: str = ""
    reason: str = ""
    evidence_count: int = 0
    expected_effect: str = ""
    risk_level: str = "low"           # low | medium | high
    status: ProposalStatus = ProposalStatus.shadow
    target_key: str = ""              # e.g. "priority.scoring.weights.urgency"

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "ts": self.ts,
            "target": self.target.value,
            "change_type": self.change_type,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
            "evidence_count": self.evidence_count,
            "expected_effect": self.expected_effect,
            "risk_level": self.risk_level,
            "status": self.status.value,
            "target_key": self.target_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyProposal":
        raw_t = data.get("target", "priority_weight")
        raw_s = data.get("status", "shadow")
        return cls(
            proposal_id=data.get("proposal_id", _uuid()),
            ts=data.get("ts", _now_iso()),
            target=ProposalTarget(raw_t) if isinstance(raw_t, str) else raw_t,
            change_type=data.get("change_type", "adjust"),
            before=data.get("before", ""),
            after=data.get("after", ""),
            reason=data.get("reason", ""),
            evidence_count=int(data.get("evidence_count", 0)),
            expected_effect=data.get("expected_effect", ""),
            risk_level=data.get("risk_level", "low"),
            status=ProposalStatus(raw_s) if isinstance(raw_s, str) else raw_s,
            target_key=data.get("target_key", ""),
        )


@dataclass
class PromotionDecision:
    """A record of whether a proposal was approved or blocked, and why."""
    decision_id: str = field(default_factory=_uuid)
    proposal_id: str = ""
    ts: str = field(default_factory=_now_iso)
    decision: str = ""             # approved | blocked | reverted
    reason: str = ""
    safety_checks: str = ""        # JSON string of check results

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "proposal_id": self.proposal_id,
            "ts": self.ts,
            "decision": self.decision,
            "reason": self.reason,
            "safety_checks": self.safety_checks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PromotionDecision":
        return cls(
            decision_id=data.get("decision_id", _uuid()),
            proposal_id=data.get("proposal_id", ""),
            ts=data.get("ts", _now_iso()),
            decision=data.get("decision", ""),
            reason=data.get("reason", ""),
            safety_checks=data.get("safety_checks", ""),
        )