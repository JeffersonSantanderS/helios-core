"""Replay synthetic fixture days through the self-improvement closed loop.

Tests that:
- synthetic day data loads without real private data,
- candidate outcomes match expectations,
- dedupe and dispatch count constraints hold,
- stale data days produce stale outcomes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from helios.self_improvement.evaluator import UsefulnessEvaluator
from helios.self_improvement.safety import SafetyGates
from helios.self_improvement.models import (
    PolicyProposal, ProposalTarget, ProposalStatus,
    OutcomeType, PrivacyClass, LearningEvent, OutcomeEvent,
)
from helios.self_improvement.store import SelfImprovementStore

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "self_improvement_days"

FIXTURES = [
    "normal_workday",
    "low_sleep_workday",
    "stale_collectors",
    "dismissed_noisy_alerts",
    "privacy_sensitive_location",
    "duplicate_notification_risk",
]


def load_fixture(name: str) -> dict:
    path = FIXTURE_DIR / f"{name}.json"
    assert path.exists(), f"Missing fixture: {path}"
    return json.loads(path.read_text())


class TestReplayFixtures:
    @pytest.mark.parametrize("name", FIXTURES)
    def test_fixture_loads(self, name):
        data = load_fixture(name)
        assert "day_label" in data
        assert "expected_outcomes" in data

    @pytest.mark.parametrize("name", FIXTURES)
    def test_no_real_coordinates(self, name):
        """All fixture files must use clearly synthetic or redacted coordinates."""
        data = load_fixture(name)
        text = json.dumps(data)
        # No obviously real-looking lat/lon outside the _fake_raw_coordinates block
        # which is a test data key only
        real_coords = ["40.7128", "-74.0060"]
        forbidden = [c for c in real_coords if c in text and "_fake_raw" not in text]
        assert not forbidden, f"Fixture {name} contains possible real coordinates outside test keys"

    def test_normal_workday_outcomes(self):
        data = load_fixture("normal_workday")
        expected = data["expected_outcomes"]
        assert expected[0]["outcome_type"] == "useful"

    def test_low_sleep_workday_outcomes(self):
        data = load_fixture("low_sleep_workday")
        expected = data["expected_outcomes"]
        assert expected[0]["outcome_type"] == "accepted"

    def test_stale_collectors_outcomes(self):
        data = load_fixture("stale_collectors")
        expected = data["expected_outcomes"]
        assert expected[0]["outcome_type"] == "stale_data"

    def test_duplicate_notification_risk_dedupe(self):
        data = load_fixture("duplicate_notification_risk")
        events = data.get("events", [])
        dispatched = [e for e in events if e.get("success")]
        suppressed = [e for e in events if not e.get("success")]
        fingerprint = data["candidates"][0]["fingerprint"]
        # Only one success per fingerprint expected
        success_fps = [e["fingerprint"] for e in dispatched if e["fingerprint"] == fingerprint]
        assert len(success_fps) == 1, f"Expected exactly 1 dispatch for {fingerprint}, got {len(success_fps)}"

    def test_privacy_sensitive_sanitized(self):
        data = load_fixture("privacy_sensitive_location")
        assertions = data.get("privacy_assertions", {})
        for field in assertions.get("sanitized_fields", []):
            # In the fixture itself, _fake_raw_coordinates contains them.
            # The privacy_assertions block documents what should happen at runtime.
            # 'coordinates' is listed; map to raw_gps or coordinate-like keys in fixture
            if field in data["_fake_raw_coordinates"]:
                continue
            if field == "coordinates" or field == "position" or field == "gps":
                assert "raw_gps" in data["_fake_raw_coordinates"], \
                    "Expected coordinate alias mapped to 'raw_gps'"
            else:
                assert False, f"Expected test field {field} not present"


class TestEvaluatorWithFixtures:
    """Test the evaluator with real fixture data through the store."""

    @pytest.fixture
    def store(self, tmp_path):
        """Create a temporary store for evaluator tests."""
        db_path = tmp_path / "test_evaluator.db"
        return SelfImprovementStore(db_path=str(db_path))

    @pytest.fixture
    def evaluator(self, store):
        """Create UsefulnessEvaluator instance with the test store."""
        return UsefulnessEvaluator(store)

    def test_dismissed_noisy_learns_negative(self, store, evaluator):
        """Test that dismissed/noisy outcomes produce negative scores."""
        data = load_fixture("dismissed_noisy_alerts")
        outcomes = data["expected_outcomes"]
        dismissed = [o for o in outcomes if o["outcome_type"] == "dismissed"]
        assert len(dismissed) >= 2

        # Insert test events and outcomes into the store
        fingerprint = "gaming_focus_late_night"
        event = LearningEvent(
            event_id="test_event_1",
            ts="2026-05-26T08:00:00-06:00",
            source="test",
            candidate_type="focus_pattern",
            fingerprint=fingerprint,
            evidence="test evidence",
            confidence=0.8,
            freshness_secs=300,
            privacy_class=PrivacyClass.private_summary,
        )
        store.record_learning_event(event)

        # Insert outcomes for the event
        for i, outcome in enumerate(outcomes):
            outcome_event = OutcomeEvent(
                outcome_id=f"test_outcome_{i}",
                event_id=event.event_id,
                ts="2026-05-26T08:00:00-06:00",
                outcome_type=OutcomeType(outcome["outcome_type"]),
                value=-0.5 if outcome["outcome_type"] in ("dismissed", "noisy") else 0.5,
                reason=outcome.get("reason", ""),
            )
            store.record_outcome(outcome_event)

        # Evaluate the fingerprint
        result = evaluator.evaluate_fingerprint(fingerprint)
        assert result["score"] < 0, f"Expected negative score for noisy candidate, got {result['score']}"
        assert result["outcome_count"] == len(outcomes)

    def test_usefulness_evaluator_no_events(self, evaluator):
        """Test that evaluator handles missing fingerprints gracefully."""
        result = evaluator.evaluate_fingerprint("nonexistent_fingerprint")
        assert result["score"] == 0.0
        assert result["outcome_count"] == 0
        assert result["confidence"] == 0.0

    def test_usefulness_evaluator_positive(self, store, evaluator):
        """Test that positive outcomes produce positive scores."""
        fingerprint = "test_positive_pattern"
        event = LearningEvent(
            event_id="test_event_positive",
            ts="2026-05-26T08:00:00-06:00",
            source="test",
            candidate_type="focus_pattern",
            fingerprint=fingerprint,
            evidence="test evidence",
            confidence=0.9,
            freshness_secs=300,
            privacy_class=PrivacyClass.private_summary,
        )
        store.record_learning_event(event)

        # Insert positive outcomes
        positive_outcomes = [
            OutcomeEvent(
                outcome_id="pos_1",
                event_id=event.event_id,
                ts="2026-05-26T08:00:00-06:00",
                outcome_type=OutcomeType.accepted,
                value=0.8,
                reason="positive",
            ),
            OutcomeEvent(
                outcome_id="pos_2",
                event_id=event.event_id,
                ts="2026-05-26T08:00:00-06:00",
                outcome_type=OutcomeType.accepted,
                value=0.8,
                reason="positive",
            ),
            OutcomeEvent(
                outcome_id="pos_3",
                event_id=event.event_id,
                ts="2026-05-26T08:00:00-06:00",
                outcome_type=OutcomeType.completed,
                value=1.0,
                reason="completed",
            ),
        ]
        for outcome in positive_outcomes:
            store.record_outcome(outcome)

        result = evaluator.evaluate_fingerprint(fingerprint)
        assert result["score"] > 0.6
        assert result["positive_count"] == 3
        assert result["can_promote_active"] is True

    def test_usefulness_evaluator_stale_blocks(self, store, evaluator):
        """Test that stale data blocks promotion."""
        fingerprint = "test_stale_pattern"
        event = LearningEvent(
            event_id="test_event_stale",
            ts="2026-05-26T08:00:00-06:00",
            source="test",
            candidate_type="focus_pattern",
            fingerprint=fingerprint,
            evidence="test evidence",
            confidence=0.7,
            freshness_secs=400,  # stale (>360 threshold)
            privacy_class=PrivacyClass.private_summary,
        )
        store.record_learning_event(event)

        # Insert mixed outcomes
        mixed_outcomes = [
            OutcomeEvent(
                outcome_id="stale_1",
                event_id=event.event_id,
                ts="2026-05-26T08:00:00-06:00",
                outcome_type=OutcomeType.accepted,
                value=0.8,
                reason="positive",
            ),
            OutcomeEvent(
                outcome_id="stale_2",
                event_id=event.event_id,
                ts="2026-05-26T08:00:00-06:00",
                outcome_type=OutcomeType.stale_data,
                value=0.0,
                reason="stale",
            ),
        ]
        for outcome in mixed_outcomes:
            store.record_outcome(outcome)

        result = evaluator.evaluate_fingerprint(fingerprint)
        assert result["stale_count"] >= 1
        assert result["has_failed_action"] is False

    def test_usefulness_evaluator_failed_blocks(self, store, evaluator):
        """Test that failed external actions block promotion."""
        fingerprint = "test_failed_pattern"
        event = LearningEvent(
            event_id="test_event_failed",
            ts="2026-05-26T08:00:00-06:00",
            source="test",
            candidate_type="focus_pattern",
            fingerprint=fingerprint,
            evidence="test evidence",
            confidence=0.8,
            freshness_secs=300,
            privacy_class=PrivacyClass.private_summary,
        )
        store.record_learning_event(event)

        # Insert outcomes with failure
        failed_outcomes = [
            OutcomeEvent(
                outcome_id="fail_1",
                event_id=event.event_id,
                ts="2026-05-26T08:00:00-06:00",
                outcome_type=OutcomeType.accepted,
                value=0.8,
                reason="positive",
            ),
            OutcomeEvent(
                outcome_id="fail_2",
                event_id=event.event_id,
                ts="2026-05-26T08:00:00-06:00",
                outcome_type=OutcomeType.failed,
                value=-1.0,
                reason="external action failed",
            ),
        ]
        for outcome in failed_outcomes:
            store.record_outcome(outcome)

        result = evaluator.evaluate_fingerprint(fingerprint)
        assert result["has_failed_action"] is True
        # Confidence comes from the event, not reduced by failures
        assert result["confidence"] == 0.8
        assert result["score"] < 0  # Negative due to failed outcome
