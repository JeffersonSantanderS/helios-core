"""Priority Engine data models."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Candidate:
    """A possible action/alert/observation Helios could surface."""

    candidate_id: str
    tick_id: str
    created_at: str
    source: str
    candidate_type: str
    title: str
    message: str = ""
    severity: str = "info"
    category: str = "system"
    priority_hint: int = 1
    module: str | None = None
    rule_slug: str | None = None
    action_name: str | None = None
    action_config: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    hydrated: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    status: str = "generated"
    fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "tick_id": self.tick_id,
            "created_at": self.created_at,
            "source": self.source,
            "candidate_type": self.candidate_type,
            "title": self.title,
            "message": self.message,
            "severity": self.severity,
            "category": self.category,
            "priority_hint": self.priority_hint,
            "module": self.module,
            "rule_slug": self.rule_slug,
            "action_name": self.action_name,
            "action_config": self.action_config,
            "raw_payload": self.raw_payload,
            "hydrated": self.hydrated,
            "tags": self.tags,
            "status": self.status,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate":
        return cls(
            candidate_id=data["candidate_id"],
            tick_id=data["tick_id"],
            created_at=data["created_at"],
            source=data["source"],
            candidate_type=data["candidate_type"],
            title=data["title"],
            message=data.get("message", ""),
            severity=data.get("severity", "info"),
            category=data.get("category", "system"),
            priority_hint=data.get("priority_hint", 1),
            module=data.get("module"),
            rule_slug=data.get("rule_slug"),
            action_name=data.get("action_name"),
            action_config=data.get("action_config", {}),
            raw_payload=data.get("raw_payload", {}),
            hydrated=data.get("hydrated", {}),
            tags=data.get("tags", []),
            status=data.get("status", "generated"),
            fingerprint=data.get("fingerprint"),
        )

    @classmethod
    def make_id(cls) -> str:
        return f"can-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def make_fingerprint(
        candidate_type: str,
        source: str,
        rule_slug: str | None = None,
        module: str | None = None,
        title: str = "",
        raw_payload: dict[str, Any] | None = None,
    ) -> str:
        """Deterministic fingerprint for repeat detection across ticks.

        Uses stable semantic identifiers (rule_slug, room, module+state)
        so the same condition produces the same fingerprint regardless of
        when it was generated.
        """
        import hashlib
        parts = [candidate_type, source]
        if rule_slug:
            parts.append(f"rule:{rule_slug}")
        if module:
            parts.append(f"mod:{module}")
        # Include stable payload keys for home sensors / module health
        payload = raw_payload or {}
        for stable_key in ("room", "threshold", "source"):
            if stable_key in payload:
                parts.append(f"{stable_key}={payload[stable_key]}")
        # Fall back to normalized title if nothing else is stable
        if not rule_slug and not module:
            normalized = title.lower().replace(" ", "_").replace("-", "_")[:40]
            parts.append(normalized)
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


@dataclass
class CandidateScore:
    """Scoring factors for a candidate."""

    candidate_id: str
    urgency: float = 0.0
    importance: float = 0.0
    relevance: float = 0.0
    confidence: float = 0.0
    context_fit: float = 0.0
    actionability: float = 0.0
    novelty: float = 0.0
    safety: float = 0.0
    disruption_cost: float = 0.0
    staleness: float = 0.0
    annoyance: float = 0.0
    redundancy: float = 0.0
    final_score: float = 0.0
    explanation: str = ""
    factors: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "urgency": round(self.urgency, 3),
            "importance": round(self.importance, 3),
            "relevance": round(self.relevance, 3),
            "confidence": round(self.confidence, 3),
            "context_fit": round(self.context_fit, 3),
            "actionability": round(self.actionability, 3),
            "novelty": round(self.novelty, 3),
            "safety": round(self.safety, 3),
            "disruption_cost": round(self.disruption_cost, 3),
            "staleness": round(self.staleness, 3),
            "annoyance": round(self.annoyance, 3),
            "redundancy": round(self.redundancy, 3),
            "final_score": round(self.final_score, 3),
            "explanation": self.explanation,
            "factors": self.factors,
        }


@dataclass
class CandidateDecision:
    """Selected disposition for a candidate."""

    candidate_id: str
    decision: str
    route: str = ""
    reason: str = ""
    final_score: float = 0.0
    threshold_used: float = 0.0
    execute_now: bool = False
    mode: str = "shadow"

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "decision": self.decision,
            "route": self.route,
            "reason": self.reason,
            "final_score": round(self.final_score, 3),
            "threshold_used": round(self.threshold_used, 3),
            "execute_now": self.execute_now,
            "mode": self.mode,
        }


@dataclass
class PriorityResult:
    """Result of evaluating one tick."""

    tick_id: str
    mode: str
    generated_count: int
    filtered_count: int
    scored_count: int
    selected_count: int
    suppressed_count: int
    deferred_count: int
    candidates: list[Candidate]
    decisions: list[CandidateDecision]
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick_id": self.tick_id,
            "mode": self.mode,
            "generated_count": self.generated_count,
            "filtered_count": self.filtered_count,
            "scored_count": self.scored_count,
            "selected_count": self.selected_count,
            "suppressed_count": self.suppressed_count,
            "deferred_count": self.deferred_count,
            "candidates": [c.to_dict() for c in self.candidates],
            "decisions": [d.to_dict() for d in self.decisions],
            "summary": self.summary,
            "error": self.error,
        }
