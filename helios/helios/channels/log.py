"""Helios LogChannel — writes events to Python logging and optional JSONL file.

A safe, zero-dependency channel for testing and debugging. No network calls,
no tokens, no external services. Events are logged at INFO level and
optionally persisted to a JSONL file for inspection.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import BaseChannel, ChannelResult
from .events import (
    AlertEvent,
    BriefingEvent,
    CheckinEvent,
    StatusEvent,
    BaseEvent,
    EventType,
)

log = logging.getLogger("helios.channels.log")

# Default JSONL path for event logging
_DEFAULT_JSONL_PATH = Path.home() / ".hermes" / "helios" / "data" / "channel_log.jsonl"


class LogChannel(BaseChannel):
    """Channel that logs events to Python logging and optional JSONL file.

    Use this for:
    - Development and testing without Matrix
    - Shadow mode event capture
    - Debugging event flow
    - Audit trail of all outbound events

    Config keys (from `channels.log`):
        enabled: bool (default True)
        jsonl_path: str (default ~/.hermes/helios/data/channel_log.jsonl)
        log_level: str (default "info" — "debug", "info", "warning")
        max_jsonl_lines: int (default 10000, 0=unlimited)
    """

    name = "log"

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        self._jsonl_path = Path(self.cfg.get("jsonl_path", str(_DEFAULT_JSONL_PATH))).expanduser()
        self._log_level = self.cfg.get("log_level", "info")
        self._max_lines = self.cfg.get("max_jsonl_lines", 10000)

    def _log_event(self, event: BaseEvent, route: str, detail: str = "") -> ChannelResult:
        """Log event to Python logger and optionally to JSONL."""
        level = getattr(logging, self._log_level.upper(), logging.INFO)
        prefix = {
            EventType.ALERT: "🚨 ALERT",
            EventType.BRIEFING: "📋 BRIEFING",
            EventType.CHECKIN: "💬 CHECKIN",
            EventType.STATUS: "📊 STATUS",
            EventType.MESSAGE: "📨 MSG",
        }.get(event.event_type, "📨 EVENT")

        msg = f"{prefix} [{route}] {event.title or event.message[:80]}"
        if detail:
            msg += f" — {detail}"
        log.log(level, msg)

        # Persist to JSONL
        self._write_jsonl(event, route)

        return ChannelResult(
            channel_name=self.name,
            success=True,
            route=route,
            detail=detail or msg,
        )

    def _write_jsonl(self, event: BaseEvent, route: str) -> None:
        """Append event to JSONL log file."""
        try:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)

            # Rotate if too large
            if self._max_lines > 0 and self._jsonl_path.exists():
                try:
                    line_count = sum(1 for _ in self._jsonl_path.open("r"))
                    if line_count >= self._max_lines:
                        # Truncate to half
                        lines = self._jsonl_path.read_text().splitlines()
                        keep = lines[-(self._max_lines // 2):]
                        self._jsonl_path.write_text("\n".join(keep) + "\n")
                except Exception:
                    pass  # Non-critical

            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event_type": event.event_type.value if isinstance(event.event_type, EventType) else str(event.event_type),
                "route": route,
                "title": event.title,
                "message": event.message[:500],  # Truncate for log
                "priority": event.priority,
                "category": event.category,
                "source": event.source,
            }

            # Add type-specific fields
            if isinstance(event, AlertEvent):
                entry["severity"] = event.severity
                entry["slug"] = event.slug
            elif isinstance(event, BriefingEvent):
                entry["briefing_type"] = event.briefing_type
            elif isinstance(event, CheckinEvent):
                entry["checkin_type"] = event.checkin_type
                if event.prompt_options:
                    entry["prompt_options"] = repr(event.prompt_options)
                if event.metadata:
                    entry["metadata"] = event.metadata

            if event.embed:
                entry["has_embed"] = True

            with self._jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            log.debug("LogChannel JSONL write failed: %s", exc)

    def _send_alert_impl(self, event: AlertEvent) -> ChannelResult:
        route = "dm" if event.priority >= 3 else "channel" if event.priority >= 2 else "log"
        return self._log_event(event, route, f"severity={event.severity}")

    def _send_briefing_impl(self, event: BriefingEvent) -> ChannelResult:
        return self._log_event(event, "briefing", f"type={event.briefing_type}")

    def _send_checkin_impl(self, event: CheckinEvent) -> ChannelResult:
        return self._log_event(event, "checkin", f"type={event.checkin_type}")

    def _send_status_impl(self, event: StatusEvent) -> ChannelResult:
        return self._log_event(event, "status")

    def _send_message_impl(self, event: BaseEvent) -> ChannelResult:
        return self._log_event(event, "message")

    def health_check(self) -> ChannelResult:
        """LogChannel always reports healthy — it's just logging."""
        return ChannelResult(
            channel_name=self.name,
            success=True,
            route="health_check",
            detail=f"LogChannel healthy — jsonl_path={self._jsonl_path}",
        )