"""Helios v6 — Reaction poller for Matrix events.

Self-hosted homeserver polling, no rate limits.

Usage:
    from helios.reaction_poller import ReactionPoller
    poller = ReactionPoller(cfg=cfg)
    poller.tick()

Registering handlers:
    poller.register_handler("mood", _my_callback)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("helios.reaction_poller")

ENV_FILE = Path.home() / ".hermes" / ".env"


def _resolve_token() -> str:
    tok = os.environ.get("MATRIX_ACCESS_TOKEN", "")
    if tok:
        return tok
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith("MATRIX_ACCESS_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _resolve_homeserver() -> str:
    srv = os.environ.get("MATRIX_HOMESERVER", "").rstrip("/")
    return srv


def _resolve_room() -> str:
    room = os.environ.get("MATRIX_HOME_ROOM", "")
    return room


ReactionHandler = Callable[[str, str, str, str], bool]


class ReactionPoller:
    """Polls Matrix room for reactions on bot messages and dispatches to handlers.

    Storage: ~/.hermes/helios/data/reaction_state.json
    """

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or {}
        self.token = _resolve_token()
        self.homeserver = _resolve_homeserver()
        self.room = _resolve_room()
        self.bot_mxid = os.environ.get("MATRIX_USER_ID", "")
        self._handlers: dict[str, list[ReactionHandler]] = {}
        self._state_dir = Path.home() / ".hermes" / "helios" / "data"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self._state_dir / "reaction_state.json"
        self._state: dict[str, Any] = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if self._state_file.exists():
            try:
                return json.loads(self._state_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "last_ts": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "processed": {},  # event_id -> {sender: emoji}
        }

    def _save_state(self) -> None:
        try:
            self._state_file.write_text(
                json.dumps(self._state, default=str, indent=2)
            )
        except OSError as exc:
            log.warning("Failed to save reaction state: %s", exc)

    def register_handler(self, tag: str, handler: ReactionHandler) -> None:
        """Register a callback for reactions matching a given tag."""
        self._handlers.setdefault(tag, []).append(handler)
        log.debug("Registered reaction handler for tag: %s", tag)

    def _api_get(self, endpoint: str, params: dict | None = None) -> dict:
        """GET a Matrix Client-Server endpoint."""
        url = f"{self.homeserver}/_matrix/client/v3/{endpoint}"
        if params:
            # Simple query builder
            q = "&".join(f"{k}={str(v).replace(' ', '%20')}" for k, v in params.items())
            url = f"{url}?{q}"

        auth_hdr = "Authorization: Bearer " + self.token
        try:
            result = subprocess.run(
                ["curl", "-s", "-S", "--max-time", "15", "-X", "GET",
                 url, "-H", auth_hdr],
                capture_output=True, text=True, timeout=20,
            )
            if result.stdout:
                return json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as exc:
            log.warning("Matrix API GET %s failed: %s", endpoint, exc)
        return {}

    def _find_bot_messages_since(self, since: datetime) -> list[dict]:
        """Return recent bot messages in the room."""
        room = self.room
        since_ts = int(since.timestamp() * 1000)
        events: list[dict] = []

        # Scan the last 100 room events backward
        data = self._api_get(f"rooms/{room}/messages", {"limit": 100, "dir": "b"})
        for ev in data.get("chunk", []):
            # Stop when we hit events older than our last check
            ts = ev.get("origin_server_ts", 0)
            if ts < since_ts:
                break

            if ev.get("type") == "m.room.message" and ev.get("sender") == self.bot_mxid:
                content = ev.get("content", {})
                body = content.get("body", "")
                if body:
                    events.append({
                        "event_id": ev["event_id"],
                        "body": body,
                        "ts": ts,
                        "content": content,
                    })
        return events

    def _find_reactions_for(self, event_id: str, since: datetime) -> list[dict]:
        """Find m.reaction events targeting `event_id` since `since`."""
        since_ts = int(since.timestamp() * 1000)
        reactions: list[dict] = []
        data = self._api_get(f"rooms/{self.room}/messages", {"limit": 200, "dir": "b"})
        for ev in data.get("chunk", []):
            if ev.get("type") != "m.reaction":
                continue
            ts = ev.get("origin_server_ts", 0)
            if ts < since_ts:
                continue
            content = ev.get("content", {})
            relates = content.get("m.relates_to", {})
            if relates.get("event_id") == event_id and relates.get("rel_type") == "m.annotation":
                reactions.append({
                    "sender": ev.get("sender", ""),
                    "emoji": relates.get("key", ""),
                    "ts": ts,
                })
        return reactions

    def tick(self) -> None:
        """Called from the daemon's main tick. Polls for new reactions."""
        if not self.token:
            log.debug("ReactionPoller: no token, skipping")
            return

        now = datetime.now(timezone.utc)
        since_str = self._state.get("last_ts", (now - timedelta(hours=1)).isoformat())
        try:
            since = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            since = now - timedelta(hours=1)

        # Resolve actual bot MXID from /account/whoami if needed
        if not self.bot_mxid:
            whoami = self._api_get("account/whoami")
            if whoami.get("user_id"):
                self.bot_mxid = whoami["user_id"]

        messages = self._find_bot_messages_since(since)
        log.debug(
            "ReactionPoller: %d bot messages since %s",
            len(messages), since.isoformat(),
        )

        processed = self._state.setdefault("processed", {})
        handled_any = False

        for msg in messages:
            msg_id = msg["event_id"]
            msg_body = msg["body"]
            msg_ts = msg["ts"]
            msg_dt = datetime.fromtimestamp(msg_ts / 1000.0, tz=timezone.utc)
            if msg_dt < since:
                continue

            reactions = self._find_reactions_for(msg_id, since)
            for react in reactions:
                sender = react["sender"]
                emoji = react["emoji"]

                if sender == self.bot_mxid:
                    continue

                # Deduplicate: only process once per (sender + reacted_msg)
                if msg_id in processed and processed[msg_id].get(sender) == emoji:
                    continue

                log.info("ReactionPoller: %s reacted %s on %s", sender, emoji, msg_id)

                # Dispatch to registered handlers by tag
                for tag, handlers in self._handlers.items():
                    # Tag matching: if the message body contains the handler tag
                    if tag.lower() not in msg_body.lower():
                        continue
                    for handler in handlers:
                        try:
                            consumed = handler(sender, msg_id, emoji, msg_body)
                            if consumed:
                                handled_any = True
                        except Exception as exc:
                            log.warning("Handler %s failed: %s", handler, exc)

                # Mark processed
                processed.setdefault(msg_id, {})[sender] = emoji

        self._state["last_ts"] = now.isoformat()
        self._save_state()
        if handled_any:
            log.info("ReactionPoller: handled reactions in this tick")
