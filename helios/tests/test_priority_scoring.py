"""Tests for Priority Engine scoring logic."""

import pytest

from helios.priority.models import Candidate
from helios.priority.config import PriorityConfig
from helios.priority.scorers import CandidateScorer


@pytest.fixture
def cfg():
    return PriorityConfig.from_raw({})


@pytest.fixture
def base_candidate():
    return Candidate(
        candidate_id="can-test",
        tick_id="tick-1",
        created_at="2026-05-16T20:00:00Z",
        source="rules_v2",
        candidate_type="rule_alert",
        title="Test Rule",
        message="Test message",
        severity="info",
        category="home",
        priority_hint=1,
        module="home",
        rule_slug="test_rule",
    )


class TestCandidateScorer:
    def test_basic_score_range(self, cfg, base_candidate):
        scorer = CandidateScorer(cfg)
        score = scorer.score(base_candidate, {})
        assert 0.0 <= score.final_score <= 1.0
        assert score.explanation != ""
        assert score.candidate_id == base_candidate.candidate_id

    def test_critical_severity_boosts_score(self, cfg, base_candidate):
        scorer = CandidateScorer(cfg)
        base_candidate.severity = "critical"
        score = scorer.score(base_candidate, {})
        assert score.urgency > 0.8
        assert score.safety >= 0.9
        assert score.final_score > 0.5  # critical should not be below median

    def test_high_priority_hint_boosts(self, cfg, base_candidate):
        scorer = CandidateScorer(cfg)
        base_candidate.priority_hint = 3
        score = scorer.score(base_candidate, {})
        # priority hint 3 adds +0.2 to urgency (capped) and +0.2 to importance
        assert score.urgency >= 0.35  # info=0.2 + boost ~0.15
        assert score.importance >= 0.7  # base ~0.6 + 0.2

    def test_quiet_hours_penalty(self, cfg, base_candidate):
        scorer = CandidateScorer(cfg)
        # Simulate quiet hours with is_quiet_hours = True
        base_candidate.hydrated = {
            "user_state": {"is_quiet_hours": True, "is_home": True},
            "alert_history": {},
        }
        score = scorer.score(base_candidate, {})
        # Disruption cost should be elevated during quiet hours
        assert score.disruption_cost > 0.5
        # Context fit should be low for non-critical during quiet hours
        assert score.context_fit <= 0.25
        # Final score should be penalized by disruption cost
        assert score.final_score < 0.5  # info alert in quiet hours shouldn't score high

    def test_driving_penalty(self, cfg, base_candidate):
        scorer = CandidateScorer(cfg)
        base_candidate.hydrated = {
            "user_state": {"is_driving": True},
            "alert_history": {},
        }
        score = scorer.score(base_candidate, {})
        assert score.disruption_cost > 0.7
        assert score.relevance < 0.6

    def test_novelty_high_for_new_alert(self, cfg, base_candidate):
        scorer = CandidateScorer(cfg)
        base_candidate.hydrated = {
            "user_state": {},
            "alert_history": {
                "same_rule_sent_1h": 0,
                "same_rule_sent_24h": 0,
            },
        }
        score = scorer.score(base_candidate, {})
        assert score.novelty >= 0.8

    def test_novelty_low_for_recent_duplicate(self, cfg, base_candidate):
        scorer = CandidateScorer(cfg)
        base_candidate.hydrated = {
            "user_state": {},
            "alert_history": {
                "same_rule_sent_1h": 1,
                "same_rule_sent_24h": 5,
            },
        }
        score = scorer.score(base_candidate, {})
        assert score.novelty <= 0.1

    def test_home_confidence_with_sensor_data(self, cfg, base_candidate):
        scorer = CandidateScorer(cfg)
        base_candidate.category = "home"
        base_candidate.hydrated = {
            "user_state": {},
            "home": {"master_bedroom_temp_c": 22.5},
        }
        score = scorer.score(base_candidate, {})
        # Having sensor data boosts confidence
        assert score.confidence > 0.7

    def test_missing_context_reduces_confidence(self, cfg, base_candidate):
        scorer = CandidateScorer(cfg)
        base_candidate.hydrated = {
            "user_state": {},
            "module_health": {"status": "unknown"},
        }
        score = scorer.score(base_candidate, {})
        assert score.confidence < 0.7

    def test_annoyance_from_repeated_alerts(self, cfg, base_candidate):
        scorer = CandidateScorer(cfg)
        base_candidate.hydrated = {
            "user_state": {},
            "alert_history": {
                "same_rule_sent_24h": 6,
                "category_sent_24h": 12,
            },
        }
        score = scorer.score(base_candidate, {})
        assert score.annoyance > 0.4

    def test_explanation_contains_factors(self, cfg, base_candidate):
        scorer = CandidateScorer(cfg)
        score = scorer.score(base_candidate, {})
        assert len(score.explanation) > 0
        assert any(
            word in score.explanation.lower()
            for word in ("moderate", "high", "low", "very high")
        )
        assert "top factors" in score.explanation.lower()


class TestScoreCalculation:
    def test_score_is_normalized_0_to_1(self, cfg):
        """Verify all scores fall in [0, 1]."""
        scorer = CandidateScorer(cfg)
        for sev in ["critical", "error", "warning", "info", "debug"]:
            for cat in ["health", "home", "system", "calendar"]:
                for ph in [1, 2, 3]:
                    c = Candidate(
                        candidate_id=f"can-{sev}-{cat}-{ph}",
                        tick_id="t",
                        created_at="2026-05-16T20:00:00Z",
                        source="rules_v2",
                        candidate_type="rule_alert",
                        title="Test",
                        severity=sev,
                        category=cat,
                        priority_hint=ph,
                    )
                    score = scorer.score(c, {})
                    assert 0.0 <= score.final_score <= 1.0, f"Failed for {sev}/{cat}/{ph}: {score.final_score}"

    def test_critical_always_higher_than_info(self, cfg):
        scorer = CandidateScorer(cfg)
        c_critical = Candidate(
            candidate_id="can-1",
            tick_id="t",
            created_at="2026-05-16T20:00:00Z",
            source="rules_v2",
            candidate_type="rule_alert",
            title="Critical",
            severity="critical",
            category="home",
        )
        c_info = Candidate(
            candidate_id="can-2",
            tick_id="t",
            created_at="2026-05-16T20:00:00Z",
            source="rules_v2",
            candidate_type="rule_alert",
            title="Info",
            severity="info",
            category="home",
        )
        s_critical = scorer.score(c_critical, {})
        s_info = scorer.score(c_info, {})
        assert s_critical.final_score > s_info.final_score
