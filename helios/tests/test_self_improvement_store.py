"""Tests for self-improvement store — SQLite persistence."""

from __future__ import annotations

import tempfile
from pathlib import Path

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
from helios.self_improvement.store import SelfImprovementStore


@pytest.fixture
def store(tmp_path):
    """Create a fresh store with a temp DB."""
    db_path = tmp_path / "test_si.db"
    s = SelfImprovementStore(db_path=str(db_path))
    yield s
    s.close()


class TestStoreIdempotentCreation:
    def test_tables_created_on_init(self, store):
        """Tables should exist after init."""
        cursor = store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row["name"] for row in cursor.fetchall()}
        assert "learning_events" in tables
        assert "outcome_events" in tables
        assert "policy_proposals" in tables
        assert "promotion_decisions" in tables

    def test_second_init_is_idempotent(self, tmp_path):
        """Creating the store twice should not fail or duplicate data."""
        db_path = tmp_path / "test_si2.db"
        s1 = SelfImprovementStore(db_path=str(db_path))
        event = LearningEvent(source="test", candidate_type="rule")
        s1.record_learning_event(event)
        s1.close()

        s2 = SelfImprovementStore(db_path=str(db_path))
        events = s2.list_recent_events()
        assert len(events) == 1
        assert events[0].source == "test"
        s2.close()

    def test_connection_reopen(self, tmp_path):
        """Data should survive connection close/reopen."""
        db_path = tmp_path / "test_si3.db"
        s1 = SelfImprovementStore(db_path=str(db_path))
        e = LearningEvent(source="persist_test")
        s1.record_learning_event(e)
        s1.close()

        s2 = SelfImprovementStore(db_path=str(db_path))
        events = s2.list_recent_events()
        assert len(events) == 1
        assert events[0].source == "persist_test"
        s2.close()


class TestLearningEventCRUD:
    def test_record_and_list(self, store):
        e = LearningEvent(
            source="priority_engine",
            candidate_type="rule_hit",
            fingerprint="fp001",
            evidence="High temp alert",
            confidence=0.9,
            freshness_secs=60.0,
            privacy_class=PrivacyClass.public_safe,
            score=0.8,
            route_decision="selected",
        )
        eid = store.record_learning_event(e)
        assert eid == e.event_id

        events = store.list_recent_events()
        assert len(events) == 1
        assert events[0].source == "priority_engine"
        assert events[0].confidence == 0.9

    def test_dedup_by_fingerprint(self, store):
        """Same fingerprint within dedup window should be deduped."""
        e1 = LearningEvent(fingerprint="dedup1", source="a")
        e2 = LearningEvent(fingerprint="dedup1", source="b")
        id1 = store.record_learning_event(e1)
        id2 = store.record_learning_event(e2)
        # Second should be deduped → same ID returned
        assert id1 == id2
        # Only one event stored
        assert len(store.list_recent_events()) == 1

    def test_different_fingerprints_stored_separately(self, store):
        e1 = LearningEvent(fingerprint="fp_a", source="a")
        e2 = LearningEvent(fingerprint="fp_b", source="b")
        store.record_learning_event(e1)
        store.record_learning_event(e2)
        assert len(store.list_recent_events()) == 2

    def test_events_survive_reopen(self, tmp_path):
        db_path = tmp_path / "test_reopen.db"
        s1 = SelfImprovementStore(db_path=str(db_path))
        s1.record_learning_event(LearningEvent(source="survive"))
        s1.close()

        s2 = SelfImprovementStore(db_path=str(db_path))
        events = s2.list_recent_events()
        assert len(events) == 1
        assert events[0].source == "survive"
        s2.close()

    def test_limit_parameter(self, store):
        for i in range(5):
            store.record_learning_event(LearningEvent(source=f"s{i}"))
        events = store.list_recent_events(limit=3)
        assert len(events) == 3


class TestOutcomeEventCRUD:
    def test_record_and_list(self, store):
        event = LearningEvent(source="test", fingerprint="oe1")
        eid = store.record_learning_event(event)

        outcome = OutcomeEvent(
            event_id=eid,
            outcome_type=OutcomeType.accepted,
            value=0.8,
            reason="User acted on suggestion",
        )
        oid = store.record_outcome(outcome)
        assert oid == outcome.outcome_id

        outcomes = store.list_outcomes()
        assert len(outcomes) == 1
        assert outcomes[0].outcome_type == OutcomeType.accepted

    def test_filter_by_event_id(self, store):
        e1 = store.record_learning_event(LearningEvent(source="a", fingerprint="oe_a"))
        e2 = store.record_learning_event(LearningEvent(source="b", fingerprint="oe_b"))

        store.record_outcome(OutcomeEvent(event_id=e1, outcome_type=OutcomeType.accepted))
        store.record_outcome(OutcomeEvent(event_id=e2, outcome_type=OutcomeType.dismissed))
        store.record_outcome(OutcomeEvent(event_id=e1, outcome_type=OutcomeType.useful))

        outcomes = store.list_outcomes(event_id=e1)
        assert len(outcomes) == 2
        assert all(o.event_id == e1 for o in outcomes)

    def test_invalid_outcome_type_raises(self, store):
        with pytest.raises(ValueError, match="Invalid outcome_type"):
            store._validate_outcome_type("not_real")


class TestPolicyProposalCRUD:
    def test_upsert_and_list(self, store):
        p = PolicyProposal(
            target=ProposalTarget.cooldown_secs,
            before="300",
            after="600",
            reason="Reduce notification fatigue",
            evidence_count=7,
            status=ProposalStatus.shadow,
        )
        pid = store.upsert_policy_proposal(p)
        proposals = store.list_proposals()
        assert len(proposals) == 1
        assert proposals[0].proposal_id == pid
        assert proposals[0].target == ProposalTarget.cooldown_secs

    def test_upsert_updates_existing(self, store):
        p = PolicyProposal(
            proposal_id="fixed_id",
            target=ProposalTarget.priority_weight,
            before="1.35",
            after="1.10",
            reason="Urgency weight too high",
            evidence_count=3,
            status=ProposalStatus.shadow,
        )
        store.upsert_policy_proposal(p)

        # Update it
        p2 = PolicyProposal(
            proposal_id="fixed_id",
            target=ProposalTarget.priority_weight,
            before="1.35",
            after="1.10",
            reason="Urgency weight too high — more evidence",
            evidence_count=12,
            status=ProposalStatus.proposed,
        )
        store.upsert_policy_proposal(p2)

        proposals = store.list_proposals()
        assert len(proposals) == 1
        assert proposals[0].evidence_count == 12
        assert proposals[0].status == ProposalStatus.proposed

    def test_filter_by_status(self, store):
        store.upsert_policy_proposal(PolicyProposal(
            proposal_id="p1", status=ProposalStatus.shadow,
        ))
        store.upsert_policy_proposal(PolicyProposal(
            proposal_id="p2", status=ProposalStatus.proposed,
        ))
        store.upsert_policy_proposal(PolicyProposal(
            proposal_id="p3", status=ProposalStatus.shadow,
        ))

        shadow = store.list_proposals(status=ProposalStatus.shadow)
        assert len(shadow) == 2
        proposed = store.list_proposals(status="proposed")
        assert len(proposed) == 1

    def test_get_proposal(self, store):
        p = PolicyProposal(proposal_id="fetch_me", reason="test")
        store.upsert_policy_proposal(p)
        fetched = store.get_proposal("fetch_me")
        assert fetched is not None
        assert fetched.reason == "test"
        assert store.get_proposal("nonexistent") is None


class TestPromotionDecisionCRUD:
    def test_record_decision(self, store):
        pid = store.upsert_policy_proposal(PolicyProposal(
            proposal_id="dec1", status=ProposalStatus.proposed,
        ))
        d = PromotionDecision(
            proposal_id=pid,
            decision="approved",
            reason="All safety checks passed",
            safety_checks='{"no_secret_payloads": true}',
        )
        did = store.record_promotion_decision(d)
        assert did == d.decision_id


class TestPrivacyAndExport:
    def test_secret_payload_stored_but_not_in_export(self, store):
        """Secret payloads are stored but must be filtered in export."""
        e = LearningEvent(
            source="test",
            evidence="Secret: API key xyz",
            privacy_class=PrivacyClass.secret,
        )
        store.record_learning_event(e)
        # The event IS stored
        events = store.list_recent_events()
        assert len(events) == 1
        assert events[0].privacy_class == PrivacyClass.secret
        # Export filtering is handled by the stable export, not the store
        # The store stores everything; export sanitization is separate

    def test_private_sensitive_stored(self, store):
        e = LearningEvent(
            source="health",
            evidence="Health data summary",
            privacy_class=PrivacyClass.private_sensitive,
        )
        store.record_learning_event(e)
        events = store.list_recent_events()
        assert events[0].privacy_class == PrivacyClass.private_sensitive


class TestCountHelpers:
    def test_count_events_24h(self, store):
        store.record_learning_event(LearningEvent(source="a", fingerprint="c1"))
        store.record_learning_event(LearningEvent(source="b", fingerprint="c2"))
        assert store.count_events_24h() == 2

    def test_count_outcomes_24h(self, store):
        eid = store.record_learning_event(LearningEvent(source="a", fingerprint="co1"))
        store.record_outcome(OutcomeEvent(event_id=eid, outcome_type=OutcomeType.accepted))
        store.record_outcome(OutcomeEvent(event_id=eid, outcome_type=OutcomeType.useful))
        assert store.count_outcomes_24h() == 2

    def test_count_proposals_by_status(self, store):
        store.upsert_policy_proposal(PolicyProposal(proposal_id="s1", status=ProposalStatus.shadow))
        store.upsert_policy_proposal(PolicyProposal(proposal_id="s2", status=ProposalStatus.shadow))
        store.upsert_policy_proposal(PolicyProposal(proposal_id="p1", status=ProposalStatus.proposed))
        counts = store.count_proposals_by_status()
        assert counts.get("shadow", 0) == 2
        assert counts.get("proposed", 0) == 1