"""Helios BaseChannel — abstract interface for all outbound channels.

Every channel implements the _send_*_impl methods for its transport.
The public send_* methods check enabled status first, then delegate.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .events import (
    AlertEvent,
    BaseEvent,
    BriefingEvent,
    CheckinEvent,
    StatusEvent,
)

log = logging.getLogger("helios.channels")


@dataclass
class ChannelResult:
    """Result from a channel send operation."""
    channel_name: str
    success: bool
    route: str = ""      # e.g. "dm", "channel", "logged", "disabled"
    detail: str = ""     # Human-readable detail or error message


class BaseChannel(ABC):
    """Abstract base class for Helios outbound channels.

    Subclass this and implement the `_send_*_impl` methods for your transport.
    Each method returns a ChannelResult indicating what happened.
    The public `send_*` methods check `enabled` first and return a disabled
    result if the channel is turned off.
    """

    name: str = "base"

    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = cfg or {}
        self._enabled: bool = self.cfg.get("enabled", True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def send(self, event: BaseEvent) -> ChannelResult:
        """Dispatch an event to the correct send_* method based on type."""
        from .events import EventType
        if isinstance(event, AlertEvent):
            return self.send_alert(event)
        elif isinstance(event, BriefingEvent):
            return self.send_briefing(event)
        elif isinstance(event, CheckinEvent):
            return self.send_checkin(event)
        elif isinstance(event, StatusEvent):
            return self.send_status(event)
        else:
            return self.send_message(event)

    # ── Public API with enabled-gating ────────────────────────────────────

    def send_alert(self, event: AlertEvent) -> ChannelResult:
        if not self._enabled:
            return ChannelResult(self.name, False, "disabled", "Channel is disabled")
        return self._send_alert_impl(event)

    def send_briefing(self, event: BriefingEvent) -> ChannelResult:
        if not self._enabled:
            return ChannelResult(self.name, False, "disabled", "Channel is disabled")
        return self._send_briefing_impl(event)

    def send_checkin(self, event: CheckinEvent) -> ChannelResult:
        if not self._enabled:
            return ChannelResult(self.name, False, "disabled", "Channel is disabled")
        return self._send_checkin_impl(event)

    def send_status(self, event: StatusEvent) -> ChannelResult:
        if not self._enabled:
            return ChannelResult(self.name, False, "disabled", "Channel is disabled")
        return self._send_status_impl(event)

    def send_message(self, event: BaseEvent) -> ChannelResult:
        if not self._enabled:
            return ChannelResult(self.name, False, "disabled", "Channel is disabled")
        return self._send_message_impl(event)

    # ── Abstract implementations — subclass must override ───────────────────

    @abstractmethod
    def _send_alert_impl(self, event: AlertEvent) -> ChannelResult: ...

    @abstractmethod
    def _send_briefing_impl(self, event: BriefingEvent) -> ChannelResult: ...

    @abstractmethod
    def _send_checkin_impl(self, event: CheckinEvent) -> ChannelResult: ...

    @abstractmethod
    def _send_status_impl(self, event: StatusEvent) -> ChannelResult: ...

    @abstractmethod
    def _send_message_impl(self, event: BaseEvent) -> ChannelResult: ...

    def health_check(self) -> ChannelResult:
        """Verify the channel can deliver. Override if transport supports it."""
        return ChannelResult(
            self.name, True, "health_check",
            "No health check implemented — assuming OK",
        )