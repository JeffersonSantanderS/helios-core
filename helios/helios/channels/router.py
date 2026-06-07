"""Helios ChannelRouter — dispatches events to enabled channels based on config.

The router is the single entry point for outbound Helios events. It reads
the `channels` config section, instantiates enabled channels, and sends
each event to all enabled channels that accept it.

Usage:
    from helios.channels import ChannelRouter, AlertEvent

    router = ChannelRouter.from_config(engine.cfg._data)
    result = router.send(AlertEvent(title="Test", message="Hello", severity="info"))

Shadow mode:
    router = ChannelRouter.from_config(cfg, shadow=True)
    # All sends are logged but no external delivery happens.
    # LogChannel still captures events for audit.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import BaseChannel, ChannelResult
from .events import BaseEvent, EventType
from .log import LogChannel
from .matrix import MatrixChannel

log = logging.getLogger("helios.channels.router")

# Registry of known channel classes by name
_CHANNEL_REGISTRY: dict[str, type[BaseChannel]] = {
    "matrix": MatrixChannel,
    "log": LogChannel,
}


class ChannelRouter:
    """Routes normalized events to enabled channels.

    Architecture:
    - Helios modules create event objects (AlertEvent, BriefingEvent, etc.)
    - The router sends each event to every enabled channel
    - Each channel decides how to deliver (DM, channel, log, etc.)
    - Results are collected per-channel for observability
    """

    def __init__(self, channels: list[BaseChannel], shadow: bool = False):
        self._channels: list[BaseChannel] = channels
        self._shadow = shadow

    @classmethod
    def from_config(cls, cfg: dict[str, Any], shadow: bool = False) -> "ChannelRouter":
        """Build a ChannelRouter from the Helios config dict.

        Config structure:
            channels:
              matrix:
                enabled: true
                # ... MatrixPusher config keys ...
              log:
                enabled: true
                jsonl_path: "~/.hermes/helios/data/channel_log.jsonl"
                log_level: "info"

        Falls back to legacy `matrix:` top-level config if `channels.matrix`
        is not found. This preserves backward compatibility.
        """
        channels_cfg = cfg.get("channels", {})

        # If no channels: section, build from legacy config
        if not channels_cfg:
            channels_cfg = cls._build_from_legacy_config(cfg)

        channels: list[BaseChannel] = []
        for channel_name, channel_cls in _CHANNEL_REGISTRY.items():
            channel_cfg = channels_cfg.get(channel_name, {})
            if not isinstance(channel_cfg, dict):
                channel_cfg = {"enabled": bool(channel_cfg)}

            # If channel_cfg doesn't explicitly set enabled, check legacy
            if "enabled" not in channel_cfg:
                # For matrix, check if legacy matrix.enabled is true
                if channel_name == "matrix":
                    legacy_enabled = cfg.get("matrix", {}).get("enabled", True)
                    channel_cfg["enabled"] = legacy_enabled
                else:
                    channel_cfg["enabled"] = True

            # For MatrixChannel, merge top-level matrix config with channels.matrix overrides
            # so MatrixPusher gets the full config (token, homeserver, room, etc.).
            # MatrixPusher uses dot-notation keys like "matrix.access_token" which
            # expect a nested dict {"matrix": {"access_token": ...}}, so we must
            # also inject the merged config under a "matrix" key for correct resolution.
            if channel_name == "matrix":
                merged_cfg = dict(cfg.get("matrix", {}))
                merged_cfg.update(channel_cfg)
                merged_cfg["matrix"] = dict(merged_cfg)
                channel_cfg = merged_cfg

            if channel_cfg.get("enabled", True):
                try:
                    channel = channel_cls(cfg=channel_cfg)
                    channels.append(channel)
                    log.info("Channel enabled: %s", channel_name)
                except Exception as exc:
                    log.warning("Failed to initialize channel %s: %s", channel_name, exc)

        # LogChannel is always available as a fallback
        has_log = any(c.name == "log" for c in channels)
        if not has_log:
            log_cfg = channels_cfg.get("log", {"enabled": True})
            channels.append(LogChannel(cfg=log_cfg))

        router = cls(channels=channels, shadow=shadow)
        log.info("ChannelRouter initialized: %d channels (shadow=%s)",
                 len(channels), shadow)
        return router

    @staticmethod
    def _build_from_legacy_config(cfg: dict[str, Any]) -> dict[str, Any]:
        """Build a channels config from the legacy top-level matrix/discord sections."""
        channels_cfg: dict[str, Any] = {}

        # Matrix from top-level config
        matrix_cfg = cfg.get("matrix", {})
        if matrix_cfg.get("enabled", True):
            channels_cfg["matrix"] = dict(matrix_cfg)

        # LogChannel always available
        channels_cfg["log"] = {"enabled": True}

        return channels_cfg

    def send(self, event: BaseEvent) -> list[ChannelResult]:
        """Send an event to all enabled channels.

        In shadow mode, only LogChannel receives the event.
        All other channels have their send() calls intercepted and
        logged as if they succeeded.

        Returns a list of ChannelResult objects, one per channel.
        """
        results: list[ChannelResult] = []

        for channel in self._channels:
            if self._shadow and channel.name != "log":
                # Shadow mode: LogChannel captures, others are suppressed
                results.append(ChannelResult(
                    channel_name=channel.name,
                    success=True,
                    route="shadow_suppressed",
                    detail=f"Shadow mode — event would go to {channel.name}",
                ))
                # Still log the shadow event
                log.info("Shadow: would send %s to %s",
                         event.event_type.value, channel.name)
                continue

            try:
                result = channel.send(event)
                results.append(result)
            except Exception as exc:
                log.warning("Channel %s send failed for %s: %s",
                            channel.name, event.event_type.value, exc)
                results.append(ChannelResult(
                    channel_name=channel.name,
                    success=False,
                    route="error",
                    detail=str(exc),
                ))

        return results

    def send_alert(self, title: str, message: str, severity: str = "info",
                   priority: int = 1, category: str = "system",
                   source: str = "", embed: dict | None = None,
                   slug: str = "", rule_description: str = "") -> list[ChannelResult]:
        """Convenience method to send an AlertEvent without constructing it."""
        from .events import AlertEvent
        event = AlertEvent(
            title=title,
            message=message,
            severity=severity,
            priority=priority,
            category=category,
            source=source,
            embed=embed,
            slug=slug,
            rule_description=rule_description,
        )
        return self.send(event)

    def send_briefing(self, title: str, body: str = "", briefing_type: str = "morning",
                      priority: int = 1, embed: dict | None = None) -> list[ChannelResult]:
        """Convenience method to send a BriefingEvent."""
        from .events import BriefingEvent
        event = BriefingEvent(
            title=title,
            message=body,
            briefing_type=briefing_type,
            priority=priority,
            embed=embed,
        )
        return self.send(event)

    def send_status(self, title: str, message: str = "", priority: int = 1,
                    category: str = "system") -> list[ChannelResult]:
        """Convenience method to send a StatusEvent."""
        from .events import StatusEvent
        event = StatusEvent(
            title=title,
            message=message,
            priority=priority,
            category=category,
        )
        return self.send(event)

    @property
    def shadow(self) -> bool:
        return self._shadow

    @shadow.setter
    def shadow(self, value: bool) -> None:
        self._shadow = value

    @property
    def channel_names(self) -> list[str]:
        """Return names of active channels."""
        return [c.name for c in self._channels]

    def health_check(self) -> list[ChannelResult]:
        """Run health checks on all enabled channels."""
        results: list[ChannelResult] = []
        for channel in self._channels:
            try:
                results.append(channel.health_check())
            except Exception as exc:
                results.append(ChannelResult(
                    channel_name=channel.name,
                    success=False,
                    route="health_check",
                    detail=str(exc),
                ))
        return results

    def __repr__(self) -> str:
        channel_names = [c.name for c in self._channels]
        return f"ChannelRouter(channels={channel_names}, shadow={self._shadow})"