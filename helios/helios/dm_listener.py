"""Helios v6 — Matrix DM Listener placeholder.

The Discord bot polling model is replaced by the Hermes Matrix gateway.
Incoming Matrix messages are routed through the normal agent pipeline.
This module now only handles outbound DM sending via MatrixPusher.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .matrix_pusher import MatrixPusher

# QueryBridge may not exist if LLM stack is not deployed
try:
    from .query_bridge import QueryBridge
except ImportError:
    QueryBridge = None

log = logging.getLogger("helios.dm_listener")


class DMListener:
    """Matrix DM wrapper. Sending via MatrixPusher; receiving is handled by gateway."""

    def __init__(self, db, cfg: Optional[Any] = None):
        self.db = db
        self.cfg = cfg or {}
        self.bridge = QueryBridge(db, cfg) if QueryBridge else None
        self.pusher = MatrixPusher(cfg)
        self._running = False

    def start(self) -> None:
        """No-op: Matrix messages come through the Hermes gateway, not polling."""
        self._running = True
        log.info("DMListener started (Matrix mode — gateway handles inbound)")

    def stop(self) -> None:
        """No-op."""
        self._running = False
        log.info("DMListener stopped")

    def poll_once(self) -> list[str]:
        """No-op in Matrix mode."""
        return []

    def process_message(self, message: str) -> str:
        """Process a message directly via QueryBridge (for gateway routing)."""
        if self.bridge:
            return self.bridge.handle_dm(message)
        return ""

    def send_response(self, message: str, priority: int = 2) -> bool:
        """Send a DM response to the user via Matrix."""
        return self.pusher.push_dm(message, priority=priority)
