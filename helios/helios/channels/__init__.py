"""Helios Channel Adapter System.

Normalized outbound delivery. Helios emits events; channels decide how to
render and deliver them. The router dispatches to enabled channels based on
config.

Usage:
    from helios.channels import ChannelRouter, AlertEvent, BriefingEvent

    router = ChannelRouter.from_config(cfg)
    router.send(AlertEvent(title="Spare Room Hot", message="...", severity="warning"))
    router.send(BriefingEvent(title="Morning Briefing", body="...", priority=1))
"""
from helios.channels.base import BaseChannel, ChannelResult
from helios.channels.events import (
    AlertEvent,
    BriefingEvent,
    CheckinEvent,
    StatusEvent,
    EventType,
)
from helios.channels.log import LogChannel
from helios.channels.matrix import MatrixChannel
from helios.channels.router import ChannelRouter

__all__ = [
    "BaseChannel",
    "ChannelRouter",
    "ChannelResult",
    "AlertEvent",
    "BriefingEvent",
    "CheckinEvent",
    "StatusEvent",
    "EventType",
    "LogChannel",
    "MatrixChannel",
]