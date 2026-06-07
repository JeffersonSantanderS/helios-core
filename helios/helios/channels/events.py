"""Helios Channel Events — normalized outbound message types.

These are the events Helios emits. Channels receive events and decide
how to render and deliver them. No channel-specific details leak into events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EventType(str, Enum):
    """Well-known event types for channel routing."""
    ALERT = "alert"
    BRIEFING = "briefing"
    CHECKIN = "checkin"
    STATUS = "status"
    MESSAGE = "message"


@dataclass
class BaseEvent:
    """Base class for all outbound events.

    Every event has a type, optional priority (0=logged, 1=low, 2=normal, 3=urgent),
    and optional embed dict for rich formatting.
    """
    event_type: EventType = EventType.MESSAGE
    title: str = ""
    message: str = ""
    priority: int = 1
    embed: Optional[dict[str, Any]] = None
    category: str = "system"
    source: str = ""


@dataclass
class AlertEvent(BaseEvent):
    """An alert from a rule hit, priority candidate, or proactive intelligence.

    Maps to MatrixChannel.push() for priority <=1, push_dm() for priority >=3.
    """
    event_type: EventType = EventType.ALERT
    severity: str = "info"  # info, warning, critical, success, system

    # Alert-specific fields
    slug: str = ""
    rule_description: str = ""


@dataclass
class BriefingEvent(BaseEvent):
    """A scheduled briefing — morning or evening.

    Maps to MatrixChannel.push() with embed.
    """
    event_type: EventType = EventType.BRIEFING
    briefing_type: str = "morning"  # morning, evening


@dataclass
class CheckinEvent(BaseEvent):
    """A check-in prompt (e.g., mood check-in).

    Maps to push_dm() or push() depending on config.
    For mood check-ins, prompt_options contains the emoji/label scale,
    and metadata carries reaction_emojis for Matrix reaction addition.
    """
    event_type: EventType = EventType.CHECKIN
    checkin_type: str = "mood"  # mood, hydration, etc.
    prompt_options: Optional[list[tuple[int, str]]] = None  # e.g., [(1, "Terrible"), (3, "Bad"), ...]
    metadata: Optional[dict[str, Any]] = None  # e.g., {"reaction_emojis": ["😭", "👎", ...]}
    html_body: Optional[str] = None  # Optional HTML formatted version


@dataclass
class StatusEvent(BaseEvent):
    """A system status message — healing, digest, summary, etc.

    Maps to push() at low priority.
    """
    event_type: EventType = EventType.STATUS