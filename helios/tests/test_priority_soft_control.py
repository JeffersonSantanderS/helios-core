"""Tests for Priority Engine Phase 4 — Soft Control."""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from helios.priority.models import Candidate, CandidateScore, CandidateDecision, PriorityResult
from helios.priority.engine import PriorityEngine


NOW = datetime.now(timezone.utc).isoformat()


class FakeDB:
    def _conn(self):
        pass


class TestGetSuppressedRuleSlugs:
    def test_returns_suppressed_rule_slugs(self):
        engine = PriorityEngine(FakeDB(), cfg={})
        cands = [
            Candidate(candidate_id="c1", tick_id="t1", created_at=NOW,
                      source="rules_v2", candidate_type="rule_alert",
                      title="A", severity="warning", category="home", rule_slug="rule_a"),
            Candidate(candidate_id="c2", tick_id="t1", created_at=NOW,
                      source="rules_v2", candidate_type="rule_alert",
                      title="B", severity="info", category="home", rule_slug="rule_b"),
            Candidate(candidate_id="c3", tick_id="t1", created_at=NOW,
                      source="home", candidate_type="home_environment_alert",
                      title="C", severity="warning", category="home", rule_slug=None),
        ]
        decisions = [
            CandidateDecision(candidate_id="c1", decision="select_notify", route="channel", reason="ok", final_score=0.8),
            CandidateDecision(candidate_id="c2", decision="suppress_duplicate", route="suppressed", reason="dup", final_score=0.3),
            CandidateDecision(candidate_id="c3", decision="suppress_low_score", route="suppressed", reason="low", final_score=0.1),
        ]
        result = PriorityResult(
            tick_id="t1", mode="shadow",
            generated_count=3, filtered_count=0, scored_count=3,
            selected_count=1, suppressed_count=2, deferred_count=0,
            candidates=cands, decisions=decisions,
            summary={},
        )
        suppressed = engine.get_suppressed_rule_slugs(result)
        assert suppressed == {"rule_b"}  # only rule with slug that was suppressed

    def test_empty_when_none_suppressed(self):
        engine = PriorityEngine(FakeDB(), cfg={})
        cands = [
            Candidate(candidate_id="c1", tick_id="t1", created_at=NOW,
                      source="rules_v2", candidate_type="rule_alert",
                      title="A", severity="warning", category="home", rule_slug="rule_a"),
        ]
        decisions = [
            CandidateDecision(candidate_id="c1", decision="select_notify", route="channel", reason="ok", final_score=0.8),
        ]
        result = PriorityResult(
            tick_id="t1", mode="shadow",
            generated_count=1, filtered_count=0, scored_count=1,
            selected_count=1, suppressed_count=0, deferred_count=0,
            candidates=cands, decisions=decisions,
            summary={},
        )
        assert engine.get_suppressed_rule_slugs(result) == set()


class TestSoftControlDispatchLogic:
    def test_dispatch_loop_skips_suppressed(self):
        """Simulate the dispatch loop with suppressed slugs."""
        dispatched = []
        rule_hits = [
            {"slug": "rule_dup", "message": "dup alert"},
            {"slug": "rule_ok", "message": "ok alert"},
        ]
        suppressed_slugs = {"rule_dup"}

        def fake_dispatch(hit, ctx):
            dispatched.append(hit.get("slug"))

        for hit in rule_hits:
            slug = hit.get("slug", "")
            if slug in suppressed_slugs:
                continue
            fake_dispatch(hit, {})

        assert dispatched == ["rule_ok"]

    def test_soft_control_enabled_flag(self):
        """Simulate config read for soft_control.enabled."""
        cfg = {"priority": {"mode": "shadow", "soft_control": {"enabled": False}}}
        soft_enabled = cfg["priority"].get("soft_control", {}).get("enabled", False)
        assert soft_enabled is False

        cfg2 = {"priority": {"mode": "shadow", "soft_control": {"enabled": True}}}
        soft_enabled2 = cfg2["priority"].get("soft_control", {}).get("enabled", False)
        assert soft_enabled2 is True


class TestCriticalNeverSuppressed:
    def test_critical_candidate_not_suppressed(self):
        from helios.priority.selector import CandidateSelector
        from helios.priority.config import PriorityConfig

        cfg = PriorityConfig()
        selector = CandidateSelector(cfg)

        critical = Candidate(candidate_id="c1", tick_id="t1", created_at=NOW,
                              source="rules_v2", candidate_type="rule_alert",
                              title="CRITICAL", severity="critical", category="system", rule_slug="rule_crit")
        score = CandidateScore(candidate_id="c1", urgency=1.0, importance=1.0, relevance=1.0,
                               confidence=1.0, context_fit=1.0, actionability=1.0,
                               novelty=0.0, safety=1.0, disruption_cost=0.0,
                               staleness=0.0, annoyance=0.0, redundancy=0.0,
                               final_score=0.15, explanation="test", factors={})

        # Even with low score, critical should NOT be suppressed
        decisions = selector.select([score], [critical], mode="shadow")
        crit_dec = [d for d in decisions if d.candidate_id == "c1"][0]
        assert not crit_dec.decision.startswith("suppress_")
        assert crit_dec.decision in ("select_notify", "select_dm", "select_log_only")

    def test_user_requested_not_suppressed(self):
        from helios.priority.selector import CandidateSelector
        from helios.priority.config import PriorityConfig

        cfg = PriorityConfig()
        selector = CandidateSelector(cfg)

        reminder = Candidate(candidate_id="c2", tick_id="t1", created_at=NOW,
                              source="rules_v2", candidate_type="rule_alert",
                              title="REMINDER", severity="info", category="tasks", rule_slug="rule_rem",
                              tags=["user_requested"])
        score = CandidateScore(candidate_id="c2", urgency=0.1, importance=0.1, relevance=0.1,
                               confidence=0.1, context_fit=0.1, actionability=0.1,
                               novelty=0.0, safety=0.0, disruption_cost=0.0,
                               staleness=0.0, annoyance=0.0, redundancy=0.0,
                               final_score=0.01, explanation="test", factors={})

        decisions = selector.select([score], [reminder], mode="shadow")
        rem_dec = [d for d in decisions if d.candidate_id == "c2"][0]
        assert not rem_dec.decision.startswith("suppress_")
        assert rem_dec.decision == "select_dm"
