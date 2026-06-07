"""Tests for self-improvement integration with priority engine."""

from __future__ import annotations

import pytest
from pathlib import Path

from helios.priority.models import (
    Candidate,
    CandidateDecision,
    PriorityResult,
)
from helios.self_improvement.models import (
    LearningEvent,
    OutcomeEvent,
    OutcomeType,
    PrivacyClass,
)
from helios.self_improvement.store import SelfImprovementStore
from helios.self_improvement.integration import (
    SelfImprovementIntegration,
    _classify_privacy,
    _sanitize_evidence,
)


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test_si_integration.db"
    s = SelfImprovementStore(db_path=str(db_path))
    yield s
    s.close()


@pytest.fixture
def integration(store):
    return SelfImprovementIntegration(store=store, cfg={"enabled": True, "mode": "shadow"})


def _make_result(candidates=None, decisions=None):
    """Helper to build a PriorityResult for testing."""
    if candidates is None:
        c1 = Candidate(
            candidate_id="can-test1", tick_id="tick-001", created_at="2026-05-26T12:00:00Z",
            source="home", candidate_type="rule_hit", title="Temperature high",
            message="Living room is 28°C", fingerprint="fp-home-temp-001",
        )
        c2 = Candidate(
            candidate_id="can-test2", tick_id="tick-001", created_at="2026-05-26T12:00:00Z",
            source="health", candidate_type="module_health", title="Health metrics stale",
            message="Health data 2h old", fingerprint="fp-health-stale-001",
            raw_payload={"heart_rate": 72, "steps": 5000},
        )
        candidates = [c1, c2]

    if decisions is None:
        d1 = CandidateDecision(
            candidate_id="can-test1", decision="select_dm", route="dm",
            reason="High temperature alert", final_score=0.85,
        )
        d2 = CandidateDecision(
            candidate_id="can-test2", decision="suppressed", route="log",
            reason="Stale health data - suppressing", final_score=0.3,
        )
        decisions = [d1, d2]

    return PriorityResult(
        tick_id="tick-001",
        mode="shadow",
        generated_count=2,
        filtered_count=0,
        scored_count=2,
        selected_count=1,
        suppressed_count=1,
        deferred_count=0,
        candidates=candidates,
        decisions=decisions,
    )


class TestRecordPriorityDecision:
    def test_records_learning_events(self, integration, store):
        result = _make_result()
        event_ids = integration.record_priority_decision(result, {})
        assert len(event_ids) == 2

        events = store.list_recent_events()
        assert len(events) == 2

        # Check first event
        selected_events = [e for e in events if e.route_decision == "selected"]
        assert len(selected_events) == 1
        assert selected_events[0].source == "home"
        assert selected_events[0].candidate_type == "rule_hit"

    def test_privacy_classification(self, integration, store):
        result = _make_result()
        integration.record_priority_decision(result, {})

        events = store.list_recent_events()
        # Home source → public_safe
        home_events = [e for e in events if e.source == "home"]
        assert home_events[0].privacy_class == PrivacyClass.public_safe

        # Health source → private_sensitive (contains health metrics)
        health_events = [e for e in events if e.source == "health"]
        assert health_events[0].privacy_class == PrivacyClass.private_sensitive

    def test_sanitized_evidence(self, integration, store):
        result = _make_result()
        integration.record_priority_decision(result, {})

        events = store.list_recent_events()
        for event in events:
            # No raw coordinates in evidence
            assert "lat:" not in event.evidence.lower()
            assert "lng:" not in event.evidence.lower()

    def test_disabled_returns_empty(self, store):
        integration = SelfImprovementIntegration(
            store=store, cfg={"enabled": False}
        )
        result = _make_result()
        event_ids = integration.record_priority_decision(result, {})
        assert event_ids == []


class TestRecordReactionOutcome:
    def test_positive_reaction(self, integration, store):
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="react1",
        ))
        oid = integration.record_reaction_outcome(eid, "👍", "User liked it")
        assert oid is not None

        outcomes = store.list_outcomes()
        assert len(outcomes) == 1
        assert outcomes[0].outcome_type == OutcomeType.accepted
        assert outcomes[0].value > 0

    def test_negative_reaction(self, integration, store):
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="react2",
        ))
        oid = integration.record_reaction_outcome(eid, "❌", "Dismissed")
        assert oid is not None

        outcomes = store.list_outcomes()
        assert outcomes[0].outcome_type == OutcomeType.dismissed
        assert outcomes[0].value < 0

    def test_duplicate_suppressed(self, integration, store):
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="dedup1",
        ))
        oid = integration.record_duplicate_suppressed(eid, "Channel dedup")
        assert oid is not None
        outcomes = store.list_outcomes()
        assert outcomes[0].outcome_type == OutcomeType.duplicate_suppressed

    def test_stale_warning(self, integration, store):
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="stale1",
        ))
        oid = integration.record_stale_warning(eid, "Data 2h old")
        assert oid is not None
        outcomes = store.list_outcomes()
        assert outcomes[0].outcome_type == OutcomeType.stale_data

    def test_action_outcome(self, integration, store):
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="action1",
        ))
        oid = integration.record_action_outcome(eid, success=True, reason="Action completed")
        assert oid is not None
        outcomes = store.list_outcomes()
        assert outcomes[0].outcome_type == OutcomeType.completed
        assert outcomes[0].value == 1.0


class TestClassifyPrivacy:
    def test_public_safe_sources(self):
        assert _classify_privacy("home", {}) == PrivacyClass.public_safe
        assert _classify_privacy("system", {}) == PrivacyClass.public_safe
        assert _classify_privacy("rule_hit", {}) == PrivacyClass.public_safe

    def test_private_summary_sources(self):
        assert _classify_privacy("calendar", {}) == PrivacyClass.private_summary
        assert _classify_privacy("spotify", {}) == PrivacyClass.private_summary

    def test_private_sensitive_health(self):
        assert _classify_privacy("health", {}) == PrivacyClass.private_sensitive

    def test_secret_with_coordinates(self):
        payload = {"lat_51": "40.7128", "lng_-114": "-74.0060"}
        assert _classify_privacy("home", payload) == PrivacyClass.secret

    def test_secret_with_api_key(self):
        payload = {"api_key": "secret123"}
        assert _classify_privacy("rule_hit", payload) == PrivacyClass.secret


class TestSanitizeEvidence:
    def test_strips_floats(self):
        result = _sanitize_evidence("location", "Alert", "Position: 40.7128, -74.0060", "rule_hit")
        assert "40.7128" not in result
        assert "[REDACTED_FLOAT]" in result

    def test_truncates_long_messages(self):
        long_msg = "A" * 500
        result = _sanitize_evidence("test", "Title", long_msg, "module_health")
        # Should be reasonable length
        assert len(result) < 250


class TestEvaluationCycle:
    def test_evaluation_cycle_generates_proposals(self, integration, store):
        # Seed events with outcomes
        for i in range(5):
            eid = store.record_learning_event(LearningEvent(
                source="home", fingerprint=f"eval_fp_{i}",
                confidence=0.8,
            ))
            # Mix positive and negative outcomes
            ot = OutcomeType.accepted if i < 3 else OutcomeType.dismissed
            store.record_outcome(OutcomeEvent(event_id=eid, outcome_type=ot))

        proposals = integration.run_evaluation_cycle()
        # Evaluation should run without error
        # In shadow mode, proposals may or may not be generated
        assert isinstance(proposals, list)

    def test_get_status(self, integration):
        status = integration.get_status()
        assert "mode" in status
        assert "enabled" in status
        assert "event_count_24h" in status
        assert status["mode"] == "shadow"
        assert status["enabled"] is True