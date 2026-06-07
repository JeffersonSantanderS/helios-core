"""Helios MatrixChannel — wraps MatrixPusher into the channel adapter interface.

This is the first production channel. It delegates to MatrixPusher for all
actual Matrix API calls, but presents a clean BaseChannel interface that
the ChannelRouter can use.

No Matrix-specific details leak OUT of this class. The router and other
channels don't know about rooms, tokens, or curl calls.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import BaseChannel, ChannelResult
from .events import (
    AlertEvent,
    BriefingEvent,
    CheckinEvent,
    StatusEvent,
    BaseEvent,
)

log = logging.getLogger("helios.channels.matrix")


class MatrixChannel(BaseChannel):
    """Channel adapter for Matrix delivery via MatrixPusher.

    Config keys (from helios config under `channels.matrix` or top-level `matrix`):
        enabled: bool (default True)
        homeserver: str
        access_token: str (or auto-detected from env)
        room: str (home room ID)
        dm_user: str (MXID for DM target)
        min_priority_to_post: int (default 1)
        min_priority_to_dm: int (default 2)
    """

    name = "matrix"

    def __repr__(self) -> str:
        cfg_display = {}
        for k, v in self.cfg.items():
            if k in ("access_token", "token"):
                cfg_display[k] = "***"
            elif k == "matrix" and isinstance(v, dict):
                cfg_display[k] = {
                    ik: "***" if ik in ("access_token", "token") else iv
                    for ik, iv in v.items()
                }
            else:
                cfg_display[k] = v
        return f"MatrixChannel(enabled={self._enabled}, cfg={cfg_display})"

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        # Import here to avoid circular imports at module level
        from ..matrix_pusher import MatrixPusher
        self._pusher = MatrixPusher(cfg=self.cfg)

    def _send_alert_impl(self, event: AlertEvent) -> ChannelResult:
        """Send an alert via Matrix. High-priority alerts go DM."""
        severity_prefix = {
            "critical": "🚨",
            "warning": "⚠️",
            "info": "ℹ️",
            "success": "✅",
            "system": "⚙️",
        }.get(event.severity, "ℹ️")

        message = f"{severity_prefix} {event.title}" if event.title else event.message
        if event.message and event.title and event.message != event.title:
            message = f"{severity_prefix} {event.title}\n{event.message}"

        if event.priority >= 3:
            # Critical/urgent → DM
            ok = self._pusher.push_dm(message, priority=event.priority, embed=event.embed)
            return ChannelResult(
                channel_name=self.name,
                success=ok,
                route="dm" if ok else "failed",
                detail=f"Alert DM: {event.title} [{event.severity}]",
            )
        elif event.priority >= 2:
            # Normal priority → channel
            ok = self._pusher.push(message, priority=event.priority, embed=event.embed)
            return ChannelResult(
                channel_name=self.name,
                success=ok,
                route="channel" if ok else "failed",
                detail=f"Alert channel: {event.title} [{event.severity}]",
            )
        else:
            # Low priority → channel (throttled by MatrixPusher)
            ok = self._pusher.push(message, priority=event.priority, embed=event.embed)
            return ChannelResult(
                channel_name=self.name,
                success=ok,
                route="low_priority" if ok else "failed",
                detail=f"Alert low-priority: {event.title} [{event.severity}]",
            )

    def _send_briefing_impl(self, event: BriefingEvent) -> ChannelResult:
        """Send a briefing (morning/evening) via Matrix channel."""
        message = event.title or f"{event.briefing_type.title()} Briefing"
        ok = self._pusher.push(message, priority=event.priority, embed=event.embed)
        return ChannelResult(
            channel_name=self.name,
            success=ok,
            route="channel" if ok else "failed",
            detail=f"Briefing: {event.briefing_type}",
        )

    def _send_checkin_impl(self, event: CheckinEvent) -> ChannelResult:
        """Send a check-in prompt via DM (preferred) or channel.

        For mood check-ins with html_body, sends as formatted Matrix message.
        Reaction addition (emoji scale buttons) is Matrix-specific and handled
        in mood_handler's send_mood_checkin() after delivery via this channel.
        """
        message = event.title or f"{event.checkin_type.title()} check-in"
        if event.message:
            message = event.message

        # If this is a mood check-in with HTML formatted body and reaction emojis,
        # delegate to mood_handler's raw curl path for full fidelity
        # (message + reaction addition). This channel handles generic check-ins.
        # Mood check-ins go through the legacy path OR through this channel
        # depending on whether the caller uses the channel system.

        # Check-ins go DM if possible, channel otherwise
        if event.html_body:
            # Build Matrix-compatible payload for rich HTML check-in
            embed = {
                "title": message,
                "description": event.message or message,
                "color": 0x3498DB,
            }
            # Merge any existing embed data
            if event.embed:
                embed.update(event.embed)
            ok = self._pusher.push(message, priority=event.priority, embed=embed)
            # Note: reaction emoji addition is Matrix-specific and stays
            # in mood_handler. This channel only delivers the prompt message.
            return ChannelResult(
                channel_name=self.name,
                success=ok,
                route="channel" if ok else "failed",
                detail=f"Checkin: {event.checkin_type}",
            )

        # Default: plain text check-in via DM
        ok = self._pusher.push_dm(message, priority=event.priority, embed=event.embed)
        if ok:
            return ChannelResult(
                channel_name=self.name,
                success=True,
                route="dm",
                detail=f"Checkin DM: {event.checkin_type}",
            )
        # DM failed or not configured — fall back to channel
        ok = self._pusher.push(message, priority=event.priority, embed=event.embed)
        return ChannelResult(
            channel_name=self.name,
            success=ok,
            route="channel" if ok else "failed",
            detail=f"Checkin channel (DM failed): {event.checkin_type}",
        )

    def _send_status_impl(self, event: StatusEvent) -> ChannelResult:
        """Send a status/system message via Matrix channel."""
        ok = self._pusher.push(event.title or event.message, priority=event.priority, embed=event.embed)
        return ChannelResult(
            channel_name=self.name,
            success=ok,
            route="channel" if ok else "failed",
            detail=f"Status: {event.title}",
        )

    def _send_message_impl(self, event: BaseEvent) -> ChannelResult:
        """Send a generic message via Matrix."""
        ok = self._pusher.push(event.message, priority=event.priority, embed=event.embed)
        return ChannelResult(
            channel_name=self.name,
            success=ok,
            route="channel" if ok else "failed",
            detail=f"Message: {event.title or event.message[:50]}",
        )

    def health_check(self) -> ChannelResult:
        """Check if MatrixPusher has a token and room configured."""
        if not self._pusher.token:
            return ChannelResult(
                channel_name=self.name,
                success=False,
                route="health_check",
                detail="No Matrix access token configured",
            )
        if not self._pusher.home_room:
            return ChannelResult(
                channel_name=self.name,
                success=False,
                route="health_check",
                detail="No Matrix home room configured",
            )
        return ChannelResult(
            channel_name=self.name,
            success=True,
            route="health_check",
            detail="Matrix pusher has token and room",
        )