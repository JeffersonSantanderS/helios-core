"""Tests for Priority Engine Phase 5 — Priority-Controlled Dispatch."""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from helios.priority.models import Candidate, CandidateScore, CandidateDecision, PriorityResult
from helios.priority.dispatcher import PriorityDispatcher

NOW = datetime.now(timezone.utc).isoformat()


class TestPriorityDispatcher:
    def test_dispatch_result_routes_dm(self):
        matrix_pusher = MagicMock()
        matrix_pusher.push_dm.return_value = True
        pd = PriorityDispatcher(matrix_pusher)

        cand = Candidate(
            candidate_id="c1", tick_id="t1", created_at=NOW,
            source="rules_v2", candidate_type="rule_alert",
            title="Test DM", message="msg", severity="critical",
            category="health", priority_hint=3, rule_slug="rule_test",
        )
        dec = CandidateDecision(
            candidate_id="c1", decision="select_dm", route="matrix_dm",
            reason="Score >= DM threshold", final_score=0.9,
        )
        result = PriorityResult(
            tick_id="t1", mode="priority_dispatch",
            generated_count=1, filtered_count=0, scored_count=1,
            selected_count=1, suppressed_count=0, deferred_count=0,
            candidates=[cand], decisions=[dec], summary={},
        )

        dispatched = pd.dispatch_result(result, {})
        assert len(dispatched) == 1
        assert dispatched[0]["route"] == "matrix_dm"
        assert dispatched[0]["sent"] is True
        matrix_pusher.push_dm.assert_called_once()
        matrix_pusher.push.assert_not_called()  # no duplicate channel push

    def test_dispatch_result_routes_channel(self):
        matrix_pusher = MagicMock()
        matrix_pusher.push.return_value = True
        pd = PriorityDispatcher(matrix_pusher)

        cand = Candidate(
            candidate_id="c2", tick_id="t1", created_at=NOW,
            source="rules_v2", candidate_type="rule_alert",
            title="Test Channel", message="msg", severity="warning",
            category="home", priority_hint=2, rule_slug="rule_test2",
        )
        dec = CandidateDecision(
            candidate_id="c2", decision="select_notify", route="matrix_channel",
            reason="Score >= notify threshold", final_score=0.75,
        )
        result = PriorityResult(
            tick_id="t1", mode="priority_dispatch",
            generated_count=1, filtered_count=0, scored_count=1,
            selected_count=1, suppressed_count=0, deferred_count=0,
            candidates=[cand], decisions=[dec], summary={},
        )

        dispatched = pd.dispatch_result(result, {})
        assert dispatched[0]["route"] == "matrix_channel"
        assert dispatched[0]["sent"] is True
        matrix_pusher.push.assert_called_once()
        matrix_pusher.push_dm.assert_not_called()  # no duplicate DM

    def test_no_duplicate_matrix_pusher_sends(self):
        """PriorityDispatcher must never call both push and push_dm for one candidate."""
        matrix_pusher = MagicMock()
        matrix_pusher.push.return_value = True
        matrix_pusher.push_dm.return_value = True
        pd = PriorityDispatcher(matrix_pusher)

        # Channel route
        c1 = Candidate(candidate_id="c1", tick_id="t1", created_at=NOW,
                       source="rules_v2", candidate_type="rule_alert",
                       title="Ch", message="m", severity="warning", category="home")
        d1 = CandidateDecision(candidate_id="c1", decision="select_notify",
                               route="matrix_channel", reason="ok", final_score=0.7)
        r1 = PriorityResult(tick_id="t1", mode="priority_dispatch",
                            generated_count=1, filtered_count=0, scored_count=1,
                            selected_count=1, suppressed_count=0, deferred_count=0,
                            candidates=[c1], decisions=[d1], summary={})
        pd.dispatch_result(r1, {})
        assert matrix_pusher.push.call_count == 1
        assert matrix_pusher.push_dm.call_count == 0

        # DM route
        c2 = Candidate(candidate_id="c2", tick_id="t1", created_at=NOW,
                       source="rules_v2", candidate_type="rule_alert",
                       title="Dm", message="m", severity="critical", category="health")
        d2 = CandidateDecision(candidate_id="c2", decision="select_dm",
                               route="matrix_dm", reason="ok", final_score=0.9)
        r2 = PriorityResult(tick_id="t1", mode="priority_dispatch",
                            generated_count=1, filtered_count=0, scored_count=1,
                            selected_count=1, suppressed_count=0, deferred_count=0,
                            candidates=[c2], decisions=[d2], summary={})
        pd.dispatch_result(r2, {})
        assert matrix_pusher.push.call_count == 1  # unchanged
        assert matrix_pusher.push_dm.call_count == 1

    def test_dispatch_result_skips_non_selected(self):
        matrix_pusher = MagicMock()
        pd = PriorityDispatcher(matrix_pusher)

        cand = Candidate(
            candidate_id="c3", tick_id="t1", created_at=NOW,
            source="rules_v2", candidate_type="rule_alert",
            title="Suppressed", message="msg", severity="info",
            category="system", priority_hint=1,
        )
        dec = CandidateDecision(
            candidate_id="c3", decision="suppress_low_score", route="suppressed",
            reason="Too low", final_score=0.1,
        )
        result = PriorityResult(
            tick_id="t1", mode="priority_dispatch",
            generated_count=1, filtered_count=0, scored_count=1,
            selected_count=0, suppressed_count=1, deferred_count=0,
            candidates=[cand], decisions=[dec], summary={},
        )

        dispatched = pd.dispatch_result(result, {})
        assert len(dispatched) == 0
        matrix_pusher.push.assert_not_called()
        matrix_pusher.push_dm.assert_not_called()

    def test_dispatch_result_summary_queues_item(self):
        """Summary route should queue the candidate and persist to disk."""
        matrix_pusher = MagicMock()
        pd = PriorityDispatcher(matrix_pusher)

        # Clear any prior test file
        from helios.priority.dispatcher import _SUMMARY_QUEUE_PATH
        if _SUMMARY_QUEUE_PATH.exists():
            _SUMMARY_QUEUE_PATH.unlink()

        cand = Candidate(
            candidate_id="c4", tick_id="t1", created_at=NOW,
            source="module_health", candidate_type="module_health_alert",
            title="Summary item", message="degraded", severity="warning",
            category="system", priority_hint=1,
            fingerprint="fp_test_123",
        )
        dec = CandidateDecision(
            candidate_id="c4", decision="select_summary", route="summary",
            reason="Score >= summary threshold", final_score=0.5,
        )
        result = PriorityResult(
            tick_id="t1", mode="priority_dispatch",
            generated_count=1, filtered_count=0, scored_count=1,
            selected_count=1, suppressed_count=0, deferred_count=0,
            candidates=[cand], decisions=[dec], summary={},
        )

        dispatched = pd.dispatch_result(result, {})
        assert len(dispatched) == 1
        assert dispatched[0]["route"] == "summary"
        assert dispatched[0]["sent"] is True
        # Queued in internal summary queue
        assert len(pd._summary_queue) == 1
        assert pd._summary_queue[0]["candidate_id"] == "c4"
        assert pd._summary_queue[0]["score"] == 0.5
        assert pd._summary_queue[0]["fingerprint"] == "fp_test_123"
        # Persisted to disk
        assert _SUMMARY_QUEUE_PATH.exists()
        lines = _SUMMARY_QUEUE_PATH.read_text().strip().splitlines()
        assert len(lines) == 1
        import json
        persisted = json.loads(lines[0])
        assert persisted["candidate_id"] == "c4"
        assert persisted["fingerprint"] == "fp_test_123"
        # No Discord push for summary route
        matrix_pusher.push.assert_not_called()
        matrix_pusher.push_dm.assert_not_called()
        # Cleanup
        _SUMMARY_QUEUE_PATH.unlink()

    def test_dispatch_result_empty(self):
        matrix_pusher = MagicMock()
        pd = PriorityDispatcher(matrix_pusher)
        result = PriorityResult(
            tick_id="t1", mode="priority_dispatch",
            generated_count=0, filtered_count=0, scored_count=0,
            selected_count=0, suppressed_count=0, deferred_count=0,
            candidates=[], decisions=[], summary={},
        )
        assert pd.dispatch_result(result, {}) == []

    def test_critical_alert_never_disappears(self):
        """A critical candidate with a select_* decision must always produce a dispatch record."""
        matrix_pusher = MagicMock()
        matrix_pusher.push.return_value = True
        pd = PriorityDispatcher(matrix_pusher)

        # Critical but low score → select_log_only route
        cand = Candidate(
            candidate_id="c_crit", tick_id="t1", created_at=NOW,
            source="rules_v2", candidate_type="rule_alert",
            title="Critical alert", message="boom", severity="critical",
            category="system", priority_hint=3, rule_slug="rule_crit",
        )
        dec = CandidateDecision(
            candidate_id="c_crit", decision="select_log_only", route="log",
            reason="Critical but low score, logging for review", final_score=0.15,
        )
        result = PriorityResult(
            tick_id="t1", mode="priority_dispatch",
            generated_count=1, filtered_count=0, scored_count=1,
            selected_count=1, suppressed_count=0, deferred_count=0,
            candidates=[cand], decisions=[dec], summary={},
        )

        dispatched = pd.dispatch_result(result, {})
        assert len(dispatched) == 1
        assert dispatched[0]["route"] == "log"
        assert dispatched[0]["sent"] is True
        # It was logged, not dropped — critical never disappears


class TestDispatchEmbeds:
    def test_embed_colors(self):
        pd = PriorityDispatcher(MagicMock())
        assert pd._severity_color("critical") == 0xE74C3C
        assert pd._severity_color("warning") == 0xF1C40F
        assert pd._severity_color("info") == 0x3498DB
        assert pd._severity_color("unknown") == 0x3498DB  # default

    def test_embed_emojis(self):
        pd = PriorityDispatcher(MagicMock())
        assert pd._severity_emoji("critical") == "🚨"
        assert pd._severity_emoji("warning") == "🔶"
        assert pd._severity_emoji("info") == "ℹ️"


# ── Phase 3: Channel Adapter Tests ─────────────────────────────────────────

class TestPriorityDispatcherChannelMirror:
    """Test that PriorityDispatcher routes through ChannelRouter as primary
    when available, with MatrixPusher fallback and no duplicate sends."""

    def _make_candidate(self, cid="c1", severity="info", route="log"):
        return Candidate(
            candidate_id=cid, tick_id="t1", created_at=NOW,
            source="rules_v2", candidate_type="rule_alert",
            title="Test Alert", message="test message",
            severity=severity, category="test", priority_hint=1,
            rule_slug="rule_test",
        ), CandidateDecision(
            candidate_id=cid, decision=f"select_{route}",
            route="matrix_dm" if route == "dm" else "matrix_channel" if route == "channel" else route,
            reason="Test dispatch", final_score=0.5,
        )

    def _make_result(self, cand, dec):
        return PriorityResult(
            tick_id="t1", mode="priority_dispatch",
            generated_count=1, filtered_count=0, scored_count=1,
            selected_count=1, suppressed_count=0, deferred_count=0,
            candidates=[cand], decisions=[dec], summary={},
        )

    def test_dispatch_with_channels_sends_via_router_not_matrix_pusher(self, tmp_path):
        """Phase 7: When channels is available, priority dispatch routes through
        ChannelRouter as primary. MatrixPusher should NOT be called (no duplicates)."""
        from helios.channels import LogChannel
        from helios.channels.base import ChannelResult

        jsonl = tmp_path / "primary_dispatch.jsonl"
        lc = LogChannel(cfg={"jsonl_path": str(jsonl), "enabled": True})

        from helios.channels.router import ChannelRouter
        mc = MagicMock()
        mc._enabled = True
        mc.name = "matrix"
        # channel.send() returns a single ChannelResult, not a list
        mc.send.return_value = ChannelResult(
            channel_name="matrix", success=True, route="channel", detail="ok"
        )

        router = ChannelRouter(channels=[lc, mc], shadow=False)

        matrix_pusher = MagicMock()
        matrix_pusher.push.return_value = True
        pd = PriorityDispatcher(matrix_pusher, channels=router)

        cand, dec = self._make_candidate(route="channel")
        result = self._make_result(cand, dec)

        dispatched = pd.dispatch_result(result, {})
        assert len(dispatched) == 1
        assert dispatched[0]["sent"] is True

        # ChannelRouter.send was called (primary delivery)
        mc.send.assert_called_once()
        # MatrixPusher should NOT be called (no duplicate)
        matrix_pusher.push.assert_not_called()

        # LogChannel received the AlertEvent
        import json
        lines = jsonl.read_text().strip().splitlines()
        assert len(lines) >= 1
        entry = json.loads(lines[0])
        assert entry["event_type"] == "alert"
        assert entry["title"] == "Test Alert"

    def test_dispatch_without_channels_preserves_behavior(self):
        """When channels=None, dispatch works exactly as before (MatrixPusher only)."""
        matrix_pusher = MagicMock()
        matrix_pusher.push.return_value = True
        pd = PriorityDispatcher(matrix_pusher, channels=None)

        cand, dec = self._make_candidate(route="channel")
        result = self._make_result(cand, dec)

        dispatched = pd.dispatch_result(result, {})
        assert len(dispatched) == 1
        assert dispatched[0]["sent"] is True
        matrix_pusher.push.assert_called_once()

    def test_dispatch_channel_failure_falls_back_to_matrix_pusher(self, tmp_path):
        """Phase 7: If ChannelRouter fails, MatrixPusher fallback still delivers."""
        from helios.channels.router import ChannelRouter
        from helios.channels.base import ChannelResult

        broken_channel = MagicMock()
        broken_channel._enabled = True
        broken_channel.name = "broken"
        # Channel returns failure result
        broken_channel.send.return_value = ChannelResult(
            channel_name="broken", success=False, route="error", detail="Connection failed"
        )

        router = ChannelRouter(channels=[broken_channel], shadow=False)

        matrix_pusher = MagicMock()
        matrix_pusher.push.return_value = True
        pd = PriorityDispatcher(matrix_pusher, channels=router)

        cand, dec = self._make_candidate(route="channel")
        result = self._make_result(cand, dec)

        # Should fall back to MatrixPusher after channel router fails
        dispatched = pd.dispatch_result(result, {})
        assert len(dispatched) == 1
        assert dispatched[0]["sent"] is True
        matrix_pusher.push.assert_called_once()

    def test_dispatch_dm_route_uses_channels_primary(self, tmp_path):
        """Phase 7: DM routes also go through ChannelRouter as primary."""
        from helios.channels.router import ChannelRouter
        from helios.channels.base import ChannelResult

        mc = MagicMock()
        mc._enabled = True
        mc.name = "matrix"
        mc.send.return_value = ChannelResult(
            channel_name="matrix", success=True, route="dm", detail="ok"
        )

        router = ChannelRouter(channels=[mc], shadow=False)

        matrix_pusher = MagicMock()
        matrix_pusher.push_dm.return_value = True
        pd = PriorityDispatcher(matrix_pusher, channels=router)

        cand, dec = self._make_candidate(severity="critical", route="dm")
        result = self._make_result(cand, dec)

        dispatched = pd.dispatch_result(result, {})
        assert len(dispatched) == 1
        assert dispatched[0]["sent"] is True
        # ChannelRouter was primary
        mc.send.assert_called_once()
        # No fallback to MatrixPusher
        matrix_pusher.push_dm.assert_not_called()

    def test_no_duplicate_matrix_sends(self, tmp_path):
        """Phase 7: When channels succeeds, there must be exactly ONE Matrix send, not two."""
        from helios.channels.router import ChannelRouter
        from helios.channels.base import ChannelResult

        mc = MagicMock()
        mc._enabled = True
        mc.name = "matrix"
        mc.send.return_value = ChannelResult(
            channel_name="matrix", success=True, route="channel", detail="ok"
        )

        router = ChannelRouter(channels=[mc], shadow=False)

        matrix_pusher = MagicMock()
        matrix_pusher.push.return_value = True
        pd = PriorityDispatcher(matrix_pusher, channels=router)

        cand, dec = self._make_candidate(route="channel")
        result = self._make_result(cand, dec)

        pd.dispatch_result(result, {})

        # Exactly one send via channels (which includes MatrixChannel)
        mc.send.assert_called_once()
        # ZERO direct MatrixPusher calls
        matrix_pusher.push.assert_not_called()
        matrix_pusher.push_dm.assert_not_called()

    def test_shadow_mode_channel_suppresses_matrix(self, tmp_path):
        """In shadow mode, ChannelRouter suppresses MatrixChannel delivery.
        PriorityDispatcher treats shadow_success as sent=True (no fallback to matrix_pusher).
        This prevents duplicate sends while still logging the event."""
        from helios.channels import LogChannel
        from helios.channels.router import ChannelRouter

        jsonl = tmp_path / "shadow_priority.jsonl"
        lc = LogChannel(cfg={"jsonl_path": str(jsonl), "enabled": True})
        mc = MagicMock()
        mc._enabled = True
        mc.name = "matrix"

        router = ChannelRouter(channels=[lc, mc], shadow=True)

        matrix_pusher = MagicMock()
        pd = PriorityDispatcher(matrix_pusher, channels=router)

        cand, dec = self._make_candidate(severity="warning", route="dm")
        result = self._make_result(cand, dec)

        # In shadow mode, ChannelRouter reports shadow_suppressed success
        # PriorityDispatcher treats this as sent=True, no fallback to matrix_pusher
        dispatched = pd.dispatch_result(result, {})
        assert len(dispatched) == 1
        assert dispatched[0]["sent"] is True  # shadow_suppressed counts as sent

        # LogChannel should have the event
        import json
        lines = jsonl.read_text().strip().splitlines()
        assert len(lines) >= 1
        entry = json.loads(lines[0])
        assert entry["event_type"] == "alert"

        # No duplicate: matrix_pusher fallback should NOT be called
        matrix_pusher.push_dm.assert_not_called()
        matrix_pusher.push.assert_not_called()
