"""Tests for Priority Engine data models."""

import pytest

from helios.priority.models import Candidate, CandidateScore, CandidateDecision, PriorityResult


class TestCandidate:
    def test_basic_construction(self):
        c = Candidate(
            candidate_id="can-abc123",
            tick_id="tick-1",
            created_at="2026-05-16T20:00:00Z",
            source="rules_v2",
            candidate_type="rule_alert",
            title="Test alert",
            message="Something happened",
            severity="warning",
            category="home",
            priority_hint=2,
            module="home",
            rule_slug="test_rule",
        )
        assert c.candidate_id == "can-abc123"
        assert c.severity == "warning"
        assert c.priority_hint == 2
        assert c.status == "generated"

    def test_to_dict_roundtrip(self):
        c = Candidate(
            candidate_id="can-abc123",
            tick_id="tick-1",
            created_at="2026-05-16T20:00:00Z",
            source="rules_v2",
            candidate_type="rule_alert",
            title="Test alert",
            severity="warning",
            category="home",
        )
        d = c.to_dict()
        c2 = Candidate.from_dict(d)
        assert c2.title == "Test alert"
        assert c2.severity == "warning"

    def test_make_id(self):
        cid = Candidate.make_id()
        assert cid.startswith("can-")
        assert len(cid) == 16


class TestCandidateScore:
    def test_to_dict_values(self):
        s = CandidateScore(
            candidate_id="can-abc",
            urgency=0.8,
            importance=0.7,
            final_score=0.75,
            explanation="Test explanation",
        )
        d = s.to_dict()
        assert d["urgency"] == 0.8
        assert d["final_score"] == 0.75
        assert "explanation" in d
        assert "factors" in d

    def test_defaults_zero(self):
        s = CandidateScore(candidate_id="can-xyz")
        assert s.urgency == 0.0
        assert s.final_score == 0.0


class TestCandidateDecision:
    def test_to_dict(self):
        d = CandidateDecision(
            candidate_id="can-abc",
            decision="select_notify",
            route="discord_channel",
            reason="Score high enough",
            final_score=0.75,
            threshold_used=0.70,
            execute_now=False,
            mode="shadow",
        )
        data = d.to_dict()
        assert data["decision"] == "select_notify"
        assert data["mode"] == "shadow"
        assert data["execute_now"] is False


class TestPriorityResult:
    def test_to_dict(self):
        c = Candidate(
            candidate_id="can-x",
            tick_id="tick-1",
            created_at="2026-05-16T20:00:00Z",
            source="rules_v2",
            candidate_type="rule_alert",
            title="Test",
        )
        r = PriorityResult(
            tick_id="tick-1",
            mode="shadow",
            generated_count=1,
            filtered_count=0,
            scored_count=1,
            selected_count=1,
            suppressed_count=0,
            deferred_count=0,
            candidates=[c],
            decisions=[],
            summary={"status": "ok"},
        )
        d = r.to_dict()
        assert d["mode"] == "shadow"
        assert d["generated_count"] == 1
        assert d["summary"] == {"status": "ok"}

    def test_error_field(self):
        r = PriorityResult(
            tick_id="tick-1",
            mode="shadow",
            generated_count=0,
            filtered_count=0,
            scored_count=0,
            selected_count=0,
            suppressed_count=0,
            deferred_count=0,
            candidates=[],
            decisions=[],
            error="Something broke",
        )
        assert r.error == "Something broke"
