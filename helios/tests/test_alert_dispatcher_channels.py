"""Tests for AlertDispatcher Phase 7B — ChannelRouter primary routing.

Tests that AlertDispatcher routes through ChannelRouter as primary,
falls back to MatrixPusher when channels fail, preserves rate limiting,
snooze, DB history, template rendering, and priority-based routing.
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from helios.dispatcher import AlertDispatcher
from helios.channels.router import ChannelRouter
from helios.channels.base import ChannelResult
from helios.channels import LogChannel
from helios.state import HeliosDB


class TestAlertDispatcherChannelRouting:
    """AlertDispatcher uses ChannelRouter as primary, MatrixPusher as fallback."""

    def _make_dispatcher(self, channels=None):
        """Create an AlertDispatcher with mock DB, mock matrix_pusher, and optional channels."""
        db = HeliosDB(":memory:")
        matrix_pusher = MagicMock()
        matrix_pusher.push.return_value = True
        matrix_pusher.push_dm.return_value = True
        return AlertDispatcher(db, matrix_pusher, config={}, channels=channels)

    def _make_hit(self, priority=1, slug="test_rule", severity="info",
                  category="system", message="Test alert"):
        return {
            "slug": slug,
            "severity": severity,
            "category": category,
            "priority": priority,
            "message": message,
            "title": "Test Alert",
        }

    def _make_channel_router(self, shadow=False, tmp_path=None):
        """Create a ChannelRouter with mock matrix and optional LogChannel."""
        mc = MagicMock()
        mc._enabled = True
        mc.name = "matrix"
        mc.send.return_value = ChannelResult(
            channel_name="matrix", success=True, route="channel", detail="ok"
        )
        channels = [mc]
        if tmp_path:
            jsonl = tmp_path / "alert_dispatch.jsonl"
            lc = LogChannel(cfg={"jsonl_path": str(jsonl), "enabled": True})
            channels.append(lc)
        return ChannelRouter(channels=channels, shadow=shadow), mc

    # ── Primary routing tests ──────────────────────────────────────────

    def test_channels_primary_no_discord(self, tmp_path):
        """When channels available, AlertDispatcher sends via channels, not discord."""
        router, mc = self._make_channel_router(tmp_path=tmp_path)
        ad = self._make_dispatcher(channels=router)
        hit = self._make_hit()

        sent = ad.dispatch(hit, {})
        assert sent is True
        mc.send.assert_called()
        ad.matrix_pusher.push.assert_not_called()
        ad.matrix_pusher.push_dm.assert_not_called()

    def test_channels_fallback_to_discord_on_failure(self):
        """When channels fail, AlertDispatcher falls back to MatrixPusher."""
        mc = MagicMock()
        mc._enabled = True
        mc.name = "matrix"
        mc.send.return_value = ChannelResult(
            channel_name="matrix", success=False, route="error", detail="failed"
        )
        router = ChannelRouter(channels=[mc], shadow=False)
        ad = self._make_dispatcher(channels=router)
        hit = self._make_hit()

        sent = ad.dispatch(hit, {})
        assert sent is True
        # Fallback to MatrixPusher
        ad.matrix_pusher.push.assert_called_once()

    def test_channels_fallback_to_discord_on_exception(self):
        """When channels raise an exception, AlertDispatcher falls back."""
        mc = MagicMock()
        mc._enabled = True
        mc.name = "matrix"
        mc.send.side_effect = RuntimeError("Connection failed")
        router = ChannelRouter(channels=[mc], shadow=False)
        ad = self._make_dispatcher(channels=router)
        hit = self._make_hit()

        sent = ad.dispatch(hit, {})
        assert sent is True
        ad.matrix_pusher.push.assert_called_once()

    def test_no_channels_uses_discord(self):
        """When channels=None, AlertDispatcher uses MatrixPusher directly."""
        ad = self._make_dispatcher(channels=None)
        hit = self._make_hit()

        sent = ad.dispatch(hit, {})
        assert sent is True
        ad.matrix_pusher.push.assert_called_once()

    # ── Priority routing tests ──────────────────────────────────────────

    def test_priority_3_uses_dm(self, tmp_path):
        """Priority >= 3 sends via DM (high urgency)."""
        router, mc = self._make_channel_router(tmp_path=tmp_path)
        ad = self._make_dispatcher(channels=router)
        hit = self._make_hit(priority=3)

        sent = ad.dispatch(hit, {})
        assert sent is True
        # Priority 3 → AlertEvent with priority=3 → MatrixChannel._send_alert_impl
        # will route to DM. We verify the event was sent with priority 3.
        calls = mc.send.call_args_list
        assert len(calls) >= 1
        event = calls[0][0][0]  # first positional arg
        assert event.priority >= 3

    def test_priority_2_sends_channel_and_dm(self, tmp_path):
        """Priority 2 sends channel message + DM (dual send)."""
        router, mc = self._make_channel_router(tmp_path=tmp_path)
        ad = self._make_dispatcher(channels=router)
        hit = self._make_hit(priority=2)

        sent = ad.dispatch(hit, {})
        assert sent is True
        # Priority 2 → two sends: channel alert + DM notification
        assert mc.send.call_count == 2
        # First send is the primary alert
        event1 = mc.send.call_args_list[0][0][0]
        assert event1.priority == 2
        # Second send is the DM notification
        event2 = mc.send.call_args_list[1][0][0]
        assert event2.priority == 3
        assert "🚨" in event2.title or "DM" in str(event2.slug) or "_dm" in str(event2.slug)

    def test_priority_1_sends_channel_only(self, tmp_path):
        """Priority < 2 sends channel message only (single send)."""
        router, mc = self._make_channel_router(tmp_path=tmp_path)
        ad = self._make_dispatcher(channels=router)
        hit = self._make_hit(priority=1)

        sent = ad.dispatch(hit, {})
        assert sent is True
        assert mc.send.call_count == 1

    def test_priority_2_legacy_fallback_dual_send(self):
        """Priority 2 fallback to MatrixPusher also does dual send."""
        ad = self._make_dispatcher(channels=None)
        hit = self._make_hit(priority=2)

        sent = ad.dispatch(hit, {})
        assert sent is True
        ad.matrix_pusher.push.assert_called_once()
        ad.matrix_pusher.push_dm.assert_called_once()

    # ── No duplicate sends ──────────────────────────────────────────────

    def test_no_duplicate_sends(self, tmp_path):
        """AlertDispatcher must NOT send via both channels and discord."""
        router, mc = self._make_channel_router(tmp_path=tmp_path)
        ad = self._make_dispatcher(channels=router)
        hit = self._make_hit()

        ad.dispatch(hit, {})
        # ChannelRouter was called
        mc.send.assert_called()
        # Discord was NOT called (no duplicate)
        ad.matrix_pusher.push.assert_not_called()
        ad.matrix_pusher.push_dm.assert_not_called()

    # ── Shadow mode ─────────────────────────────────────────────────────

    def test_shadow_mode_suppresses_matrix(self, tmp_path):
        """In shadow mode, ChannelRouter suppresses Matrix delivery.
        AlertDispatcher treats shadow_suppressed as sent=True."""
        router, mc = self._make_channel_router(shadow=True, tmp_path=tmp_path)
        ad = self._make_dispatcher(channels=router)
        hit = self._make_hit()

        sent = ad.dispatch(hit, {})
        # Shadow mode returns success=True with route="shadow_suppressed"
        assert sent is True
        # No discord fallback
        ad.matrix_pusher.push.assert_not_called()

    # ── Rate limiting preserved ────────────────────────────────────────

    def test_rate_limit_suppresses_send(self, tmp_path):
        """Rate-limited alerts do NOT send through channels or discord."""
        router, mc = self._make_channel_router(tmp_path=tmp_path)
        config = {"alerts": {"max_per_rule_per_hour": 1, "max_total_per_hour": 10, "rate_window_secs": 3600}}
        db = HeliosDB(":memory:")
        discord = MagicMock()
        ad = AlertDispatcher(db, discord, config=config, channels=router)

        hit = self._make_hit(slug="rate_limited_rule")
        sent1 = ad.dispatch(hit, {})
        assert sent1 is True

        # Second dispatch within rate window should be suppressed
        sent2 = ad.dispatch(hit, {})
        assert sent2 is False
        mc.send.assert_called_once()  # Only one send
        discord.push.assert_not_called()  # No fallback either

    def test_global_rate_limit_suppresses_send(self):
        """Global rate limit also suppresses regardless of channel path."""
        config = {"alerts": {"max_per_rule_per_hour": 999, "max_total_per_hour": 1, "rate_window_secs": 3600}}
        db = HeliosDB(":memory:")
        discord = MagicMock()
        ad = AlertDispatcher(db, discord, config=config, channels=None)

        hit1 = self._make_hit(slug="rule_a")
        hit2 = self._make_hit(slug="rule_b")

        ad.dispatch(hit1, {})
        sent2 = ad.dispatch(hit2, {})
        assert sent2 is False

    # ── Snooze preserved ────────────────────────────────────────────────

    def test_snoozed_alert_not_sent(self, tmp_path):
        """Snoozed alerts do NOT send through channels or discord."""
        router, mc = self._make_channel_router(tmp_path=tmp_path)
        ad = self._make_dispatcher(channels=router)

        hit = self._make_hit(slug="snoozed_rule")
        ad.snooze("snoozed_rule", 10)

        sent = ad.dispatch(hit, {})
        assert sent is False
        mc.send.assert_not_called()
        ad.matrix_pusher.push.assert_not_called()
        ad.matrix_pusher.push_dm.assert_not_called()

    # ── DB history logging preserved ────────────────────────────────────

    def test_db_history_logged_on_send(self, tmp_path):
        """DB alert history is logged whether sent via channels or discord."""
        router, mc = self._make_channel_router(tmp_path=tmp_path)
        ad = self._make_dispatcher(channels=router)
        hit = self._make_hit(slug="db_test_rule")

        ad.dispatch(hit, {})
        recent = ad.get_recent_alerts(limit=5)
        matching = [a for a in recent if a.get("rule_slug") == "db_test_rule"]
        assert len(matching) == 1
        assert matching[0]["sent"] == 1

    def test_db_history_logged_on_failure(self):
        """DB alert history records sent=0 when delivery fails."""
        mc = MagicMock()
        mc._enabled = True
        mc.name = "matrix"
        mc.send.return_value = ChannelResult(
            channel_name="matrix", success=False, route="error", detail="failed"
        )
        router = ChannelRouter(channels=[mc], shadow=False)
        ad = self._make_dispatcher(channels=router)
        # Also make fallback discord fail
        ad.matrix_pusher.push.return_value = False

        hit = self._make_hit(slug="fail_test_rule")
        sent = ad.dispatch(hit, {})
        assert sent is False
        recent = ad.get_recent_alerts(limit=5)
        matching = [a for a in recent if a.get("rule_slug") == "fail_test_rule"]
        assert len(matching) == 1
        assert matching[0]["sent"] == 0

    def test_db_history_logged_on_rate_limit(self):
        """DB history is NOT logged for rate-limited alerts (they return False early)."""
        ad = self._make_dispatcher(channels=None)
        config = {"alerts": {"max_per_rule_per_hour": 1, "max_total_per_hour": 10, "rate_window_secs": 3600}}
        ad.max_per_rule_per_hour = 1

        hit = self._make_hit(slug="rate_rule")
        ad.dispatch(hit, {})  # First dispatch logs to DB
        ad.dispatch(hit, {})  # Second dispatch rate-limited, no DB log
        recent = ad.get_recent_alerts(limit=5)
        matching = [a for a in recent if a.get("rule_slug") == "rate_rule"]
        assert len(matching) == 1  # Only one DB entry, not two

    # ── Template rendering preserved ────────────────────────────────────

    def test_template_render_failure_never_sends(self, tmp_path):
        """When template rendering fails, no channel or discord send occurs."""
        router, mc = self._make_channel_router(tmp_path=tmp_path)
        ad = self._make_dispatcher(channels=router)
        hit = self._make_hit(slug="bad_template", message="{missing.key}")

        sent = ad.dispatch(hit, {})
        assert sent is False
        mc.send.assert_not_called()
        ad.matrix_pusher.push.assert_not_called()
        ad.matrix_pusher.push_dm.assert_not_called()

    # ── LogChannel audit ────────────────────────────────────────────────

    def test_logchannel_receives_alert_event(self, tmp_path):
        """LogChannel captures alert events via ChannelRouter."""
        jsonl = tmp_path / "alert_audit.jsonl"
        lc = LogChannel(cfg={"jsonl_path": str(jsonl), "enabled": True})
        mc = MagicMock()
        mc._enabled = True
        mc.name = "matrix"
        mc.send.return_value = ChannelResult(
            channel_name="matrix", success=True, route="channel", detail="ok"
        )
        router = ChannelRouter(channels=[lc, mc], shadow=False)

        ad = self._make_dispatcher(channels=router)
        hit = self._make_hit(slug="audit_test", severity="warning")

        ad.dispatch(hit, {})
        lines = jsonl.read_text().strip().splitlines()
        assert len(lines) >= 1
        entry = json.loads(lines[0])
        assert entry["event_type"] == "alert"
        assert "audit_test" in str(entry.get("slug", ""))