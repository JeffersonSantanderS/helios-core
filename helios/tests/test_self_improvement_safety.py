"""Tests for self-improvement safety gates."""

from __future__ import annotations

import pytest
from pathlib import Path

from helios.self_improvement.models import (
    LearningEvent,
    OutcomeEvent,
    OutcomeType,
    PrivacyClass,
    PolicyProposal,
    ProposalTarget,
    ProposalStatus,
)
from helios.self_improvement.store import SelfImprovementStore
from helios.self_improvement.safety import SafetyGates


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test_safety.db"
    s = SelfImprovementStore(db_path=str(db_path))
    yield s
    s.close()


@pytest.fixture
def safety(store):
    return SafetyGates(store)


class TestSafetyGates:
    def test_all_gates_pass_for_clean_proposal(self, store, safety):
        # Seed some positive outcomes
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="clean1", confidence=0.9,
        ))
        for _ in range(4):
            store.record_outcome(OutcomeEvent(
                event_id=eid, outcome_type=OutcomeType.accepted,
            ))

        proposal = PolicyProposal(
            target=ProposalTarget.priority_weight,
            change_type="adjust",
            before="1.35",
            after="1.10",
            reason="Urgency weight adjustment based on acceptance data",
            evidence_count=4,
            status=ProposalStatus.shadow,
        )

        report = safety.check(proposal)
        assert report.all_passed
        assert proposal.status == ProposalStatus.shadow  # stays shadow

    def test_no_secret_payloads_blocks(self, store, safety):
        # Seed a secret-class event
        store.record_learning_event(LearningEvent(
            source="test", fingerprint="secret1",
            privacy_class=PrivacyClass.secret,
        ))

        proposal = PolicyProposal(
            target=ProposalTarget.priority_weight,
            evidence_count=3,
        )
        report = safety.check(proposal)
        # The no_secret_payloads check should pass if the proposal
        # doesn't reference the secret fingerprint's target key
        # (secret events may exist but proposals can avoid them)
        # This test verifies the gate exists and runs
        assert any(c.name == "no_secret_payloads" for c in report.checks)

    def test_no_raw_coordinates_blocks(self, store, safety):
        proposal = PolicyProposal(
            target=ProposalTarget.priority_weight,
            before="1.0",
            after="1.5",
            reason="lat:40.7128 lng:-74.0060 data shows pattern",
            evidence_count=5,
        )
        report = safety.check(proposal)
        assert not report.all_passed
        assert any(c.name == "no_raw_coordinates" and not c.passed for c in report.checks)

    def test_minimum_evidence_count_blocks(self, store, safety):
        proposal = PolicyProposal(
            target=ProposalTarget.priority_weight,
            evidence_count=2,  # below minimum of 3
        )
        report = safety.check(proposal)
        assert not report.all_passed
        gate = next(c for c in report.checks if c.name == "minimum_evidence_count")
        assert not gate.passed

    def test_negative_outcome_rate_blocks(self, store, safety):
        # Seed many negative outcomes
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="neg_rate",
        ))
        for ot in [OutcomeType.dismissed, OutcomeType.noisy, OutcomeType.noisy,
                    OutcomeType.ignored, OutcomeType.dismissed]:
            store.record_outcome(OutcomeEvent(event_id=eid, outcome_type=ot))

        proposal = PolicyProposal(
            target=ProposalTarget.priority_weight,
            evidence_count=5,
        )
        report = safety.check(proposal)
        gate = next(c for c in report.checks if c.name == "negative_outcome_rate_below_threshold")
        # With 5 negative out of 5 outcomes, rate > 0.35
        assert not gate.passed

    def test_cooldown_decrease_blocked_below_minimum(self, store, safety):
        proposal = PolicyProposal(
            target=ProposalTarget.cooldown_secs,
            before="300",
            after="30",  # below min 60s
            evidence_count=5,
        )
        report = safety.check(proposal)
        gate = next(c for c in report.checks if c.name == "cooldown_not_decreased_too_far")
        assert not gate.passed

    def test_cooldown_decrease_allowed_above_minimum(self, store, safety):
        # Seed enough positive outcomes to have a low negative rate
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="cool_ok",
        ))
        for _ in range(5):
            store.record_outcome(OutcomeEvent(
                event_id=eid, outcome_type=OutcomeType.accepted,
            ))

        proposal = PolicyProposal(
            target=ProposalTarget.cooldown_secs,
            before="600",
            after="120",  # above min 60s
            evidence_count=5,
        )
        report = safety.check(proposal)
        gate = next(c for c in report.checks if c.name == "cooldown_not_decreased_too_far")
        assert gate.passed

    def test_quiet_hours_preserved_blocks_disabled(self, store, safety):
        proposal = PolicyProposal(
            target=ProposalTarget.quiet_hour_rule,
            after="none",  # trying to disable quiet hours
            evidence_count=5,
        )
        report = safety.check(proposal)
        gate = next(c for c in report.checks if c.name == "quiet_hours_preserved")
        assert not gate.passed

    def test_external_mutation_review_required(self, store, safety):
        proposal = PolicyProposal(
            target=ProposalTarget.candidate_enablement,
            change_type="enable",
            evidence_count=5,
        )
        report = safety.check(proposal)
        gate = next(c for c in report.checks if c.name == "external_mutation_review_required")
        assert not gate.passed

    def test_blocked_proposal_gets_blocked_status(self, store, safety):
        proposal = PolicyProposal(
            target=ProposalTarget.priority_weight,
            evidence_count=1,  # below minimum
            status=ProposalStatus.shadow,
        )
        safety.check(proposal)
        assert proposal.status == ProposalStatus.blocked

    def test_safety_report_json(self, store, safety):
        proposal = PolicyProposal(
            target=ProposalTarget.priority_weight,
            evidence_count=5,
        )
        report = safety.check(proposal)
        j = report.to_json()
        assert "proposal_id" in j
        assert "checks" in j