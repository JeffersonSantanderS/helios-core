"""Tests for self-improvement evaluator and scoring."""

from __future__ import annotations

import pytest
from pathlib import Path

from helios.self_improvement.models import (
    LearningEvent,
    OutcomeEvent,
    OutcomeType,
    PrivacyClass,
)
from helios.self_improvement.store import SelfImprovementStore
from helios.self_improvement.evaluator import UsefulnessEvaluator


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test_eval.db"
    s = SelfImprovementStore(db_path=str(db_path))
    yield s
    s.close()


@pytest.fixture
def evaluator(store):
    return UsefulnessEvaluator(store)


class TestUsefulnessScoring:
    def test_all_positive_outcomes_high_score(self, store, evaluator):
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="pos1", confidence=0.9,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.accepted, value=0.8,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.completed, value=1.0,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.useful, value=0.6,
        ))

        result = evaluator.evaluate_fingerprint("pos1")
        assert result["score"] > 0.5
        assert result["positive_count"] == 3
        assert result["negative_count"] == 0
        assert result["can_promote_active"] is True

    def test_all_negative_outcomes_low_score(self, store, evaluator):
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="neg1", confidence=0.7,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.dismissed, value=-0.5,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.noisy, value=-0.7,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.ignored, value=-0.2,
        ))

        result = evaluator.evaluate_fingerprint("neg1")
        assert result["score"] < 0
        assert result["negative_count"] == 3

    def test_mixed_outcomes(self, store, evaluator):
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="mix1", confidence=0.8,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.accepted,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.dismissed,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.useful,
        ))

        result = evaluator.evaluate_fingerprint("mix1")
        # Mixed — score should be moderate
        assert -1.0 <= result["score"] <= 1.0

    def test_unknown_fingerprint(self, store, evaluator):
        result = evaluator.evaluate_fingerprint("nonexistent")
        assert result["score"] == 0.0
        assert result["outcome_count"] == 0
        assert result["can_promote_active"] is False

    def test_stale_data_caps_confidence(self, store, evaluator):
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="stale1", confidence=0.9,
            freshness_secs=7200.0,  # 2 hours old → stale
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.accepted,
        ))

        result = evaluator.evaluate_fingerprint("stale1")
        # Confidence should be capped at 0.35 for stale data
        assert result["confidence"] <= 0.35

    def test_failed_action_blocks_promotion(self, store, evaluator):
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="fail1", confidence=0.9,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.failed,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.accepted,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.completed,
        ))

        result = evaluator.evaluate_fingerprint("fail1")
        assert result["has_failed_action"] is True
        assert result["can_promote_active"] is False

    def test_minimum_evidence_for_promotion(self, store, evaluator):
        eid = store.record_learning_event(LearningEvent(
            source="test", fingerprint="few1", confidence=0.9,
        ))
        # Only 2 outcomes — not enough for active promotion
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.accepted,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid, outcome_type=OutcomeType.completed,
        ))

        result = evaluator.evaluate_fingerprint("few1")
        assert result["can_promote_active"] is False

    def test_evaluate_recent(self, store, evaluator):
        # Create events with different fingerprints
        eid1 = store.record_learning_event(LearningEvent(
            source="test", fingerprint="recent1", confidence=0.8,
        ))
        eid2 = store.record_learning_event(LearningEvent(
            source="test", fingerprint="recent2", confidence=0.7,
        ))

        store.record_outcome(OutcomeEvent(
            event_id=eid1, outcome_type=OutcomeType.accepted,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid2, outcome_type=OutcomeType.dismissed,
        ))
        store.record_outcome(OutcomeEvent(
            event_id=eid1, outcome_type=OutcomeType.completed,
        ))

        results = evaluator.evaluate_recent()
        assert len(results) >= 2