"""Tests for self-improvement data models."""

from __future__ import annotations

import pytest
from helios.self_improvement.models import (
    LearningEvent,
    OutcomeEvent,
    OutcomeType,
    PrivacyClass,
    PolicyProposal,
    ProposalTarget,
    ProposalStatus,
    PromotionDecision,
)


class TestLearningEvent:
    def test_defaults(self):
        e = LearningEvent()
        assert e.event_id  # auto-generated
        assert e.ts  # auto-generated
        assert e.source == ""
        assert e.confidence == 0.0
        assert e.privacy_class == PrivacyClass.public_safe
        assert e.route_decision == ""

    def test_to_dict_round_trip(self):
        e = LearningEvent(
            source="priority_engine",
            candidate_type="rule_hit",
            fingerprint="abc123",
            evidence="Temperature alert: 28°C",
            confidence=0.85,
            freshness_secs=120.0,
            privacy_class=PrivacyClass.public_safe,
            score=0.72,
            route_decision="selected",
        )
        d = e.to_dict()
        assert d["source"] == "priority_engine"
        assert d["fingerprint"] == "abc123"
        assert d["privacy_class"] == "public_safe"

        e2 = LearningEvent.from_dict(d)
        assert e2.source == e.source
        assert e2.fingerprint == e.fingerprint
        assert e2.confidence == e.confidence
        assert e2.privacy_class == PrivacyClass.public_safe

    def test_from_dict_with_string_privacy_class(self):
        d = {"privacy_class": "private_sensitive"}
        e = LearningEvent.from_dict(d)
        assert e.privacy_class == PrivacyClass.private_sensitive


class TestOutcomeEvent:
    def test_defaults(self):
        o = OutcomeEvent()
        assert o.outcome_id
        assert o.outcome_type == OutcomeType.ignored

    def test_all_outcome_types(self):
        for ot in OutcomeType:
            o = OutcomeEvent(outcome_type=ot)
            d = o.to_dict()
            o2 = OutcomeEvent.from_dict(d)
            assert o2.outcome_type == ot

    def test_invalid_outcome_type_raises(self):
        with pytest.raises(ValueError):
            OutcomeType("not_a_real_type")

    def test_negative_value(self):
        o = OutcomeEvent(outcome_type=OutcomeType.dismissed, value=-0.5, reason="User swiped away")
        assert o.value == -0.5
        d = o.to_dict()
        o2 = OutcomeEvent.from_dict(d)
        assert o2.value == -0.5


class TestPolicyProposal:
    def test_defaults(self):
        p = PolicyProposal()
        assert p.status == ProposalStatus.shadow
        assert p.target == ProposalTarget.priority_weight
        assert p.risk_level == "low"
        assert p.change_type == "adjust"

    def test_round_trip(self):
        p = PolicyProposal(
            target=ProposalTarget.cooldown_secs,
            change_type="adjust",
            before="300",
            after="600",
            reason="Users dismissed 5+ notifications within 5 minutes",
            evidence_count=7,
            expected_effect="Reduce notification fatigue during work hours",
            risk_level="low",
            status=ProposalStatus.shadow,
            target_key="priority.scoring.weights.urgency",
        )
        d = p.to_dict()
        p2 = PolicyProposal.from_dict(d)
        assert p2.target == ProposalTarget.cooldown_secs
        assert p2.evidence_count == 7
        assert p2.before == "300"


class TestPromotionDecision:
    def test_approved(self):
        d = PromotionDecision(
            proposal_id="prop1",
            decision="approved",
            reason="All safety checks passed",
            safety_checks='{"no_secret_payloads": true, "minimum_evidence_count": true}',
        )
        assert d.decision == "approved"

    def test_blocked(self):
        d = PromotionDecision(
            proposal_id="prop2",
            decision="blocked",
            reason="Negative outcome rate too high",
            safety_checks='{"negative_outcome_rate_below_threshold": false}',
        )
        assert d.decision == "blocked"

    def test_round_trip(self):
        d = PromotionDecision(
            proposal_id="prop1",
            decision="approved",
            reason="OK",
            safety_checks='{"all": true}',
        )
        d2 = PromotionDecision.from_dict(d.to_dict())
        assert d2.proposal_id == "prop1"
        assert d2.decision == "approved"


class TestPrivacyClass:
    def test_all_classes(self):
        expected = {"public_safe", "private_summary", "private_sensitive", "secret"}
        actual = {c.value for c in PrivacyClass}
        assert actual == expected

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            PrivacyClass("top_secret")


class TestOutcomeType:
    def test_all_types_exist(self):
        expected = {
            "accepted", "dismissed", "ignored", "snoozed", "completed",
            "failed", "stale_data", "duplicate_suppressed", "noisy", "useful",
        }
        actual = {t.value for t in OutcomeType}
        assert actual == expected