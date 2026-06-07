"""Helios v5 — Discord push via curl subprocess (avoids urllib Discord error 1010)."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("helios.discord_push")


class DiscordPusher:
    BASE_URL = "https://discord.com/api/v10"

    def __init__(self, cfg: Optional[Any] = None):
        self.cfg = cfg or {}
        self.enabled = self._resolve_bool(self.cfg, "discord.enabled", True)
        self.token = self._resolve_str(self.cfg, "discord.bot_token", "")
        self.user_id = self._resolve_str(self.cfg, "discord.dm.user_id", "")
        self.channel_id = self._resolve_str(self.cfg, "discord.channels.helios.id", "")

        if not self.token:
            self.token = self._auto_detect_token()

        self._last_push_ts: float = 0.0

    @staticmethod
    def _resolve_bool(cfg, key, default):
        v = cfg
        for part in key.split("."):
            v = v.get(part, {}) if isinstance(v, dict) else {}
        return v if isinstance(v, bool) else default

    @staticmethod
    def _resolve_str(cfg, key, default):
        v = cfg
        for part in key.split("."):
            v = v.get(part, {}) if isinstance(v, dict) else {}
        return (v if isinstance(v, str) and v else default) or default

    def _auto_detect_token(self) -> str:
        for env_var in ("HELIOS_DISCORD_BOT_TOKEN", "DISCORD_BOT_TOKEN"):
            token = os.environ.get(env_var, "")
            if token and token.strip():
                log.info("Using Discord token from $%s", env_var)
                return token.strip()

        env_paths = [
            Path.home() / ".hermes" / ".env",
            Path.home() / ".hermes" / "config" / ".env",
        ]
        for env_path in env_paths:
            if env_path.exists():
                try:
                    for line in env_path.read_text().splitlines():
                        line = line.strip()
                        if line.startswith("DISCORD_BOT_TOKEN="):
                            token = line.split("=", 1)[1].strip().strip('"').strip("'")
                            if token:
                                log.info("Using Discord token from %s", env_path)
                                return token
                except Exception:
                    pass

        # P0: Also check user's main Hermes profiles
        profile_envs = sorted(Path.home().rglob(".hermes/profiles/*/.env"))
        for env_path in profile_envs:
            try:
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("DISCORD_BOT_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if token:
                            log.info("Using Discord token from profile %s", env_path.parent.name)
                            return token
            except Exception:
                pass

        log.warning("No Discord bot token found")
        return ""

    def _curl(self, url: str, payload: dict, timeout: int = 15) -> bool:
        """Send via curl subprocess."""
        try:
            data = json.dumps(payload)
            result = subprocess.run(
                ["curl", "-s", "-w", "%{http_code}", "-X", "POST",
                 "-H", f"Authorization: Bot {self.token}",
                 "-H", "Content-Type: application/json",
                 "-d", data,
                 "--connect-timeout", "5",
                 "--max-time", str(timeout),
                 url],
                capture_output=True, text=True, timeout=timeout + 5
            )
            stdout = result.stdout
            status_code = 0
            if len(stdout) >= 3:
                try:
                    status_code = int(stdout[-3:])
                except ValueError:
                    pass

            if status_code == 200:
                log.info("Discord push OK")
                return True
            else:
                body = stdout[:-3] if len(stdout) >= 3 else stdout
                log.warning("Discord push failed: HTTP %s", status_code)
                return False
        except Exception as exc:
            log.warning("Discord curl failed: %s", exc)
            return False

    def push_routed(self, message: str, priority: int, source: str = "") -> tuple:
        """Route a message based on priority.

        Priority 3 -> DM (urgent)
        Priority 2 -> Channel push
        Priority 1 -> Channel push (throttled)
        Priority 0 -> Log only

        Returns (sent, route_used).
        """
        import time as _time
        if priority >= 3:
            ok = self.push_dm(f"🚨 {message}", priority=priority)
            return (ok, "dm" if ok else "failed")
        if priority >= 2:
            ok = self.push(message, priority=priority)
            return (ok, "channel" if ok else "failed")
        if priority >= 1:
            now = _time.time()
            if now - self._last_push_ts < 60:
                log.debug("Throttled push suppressed (last was %.0fs ago)", now - self._last_push_ts)
                return (False, "throttled")
            self._last_push_ts = now
            ok = self.push(message, priority=priority)
            return (ok, "channel_throttled")
        log.debug("[%s] %s", source, message[:100])
        return (False, "logged")

    def push(self, message: str, priority: int = 0, embed: Optional[dict] = None) -> bool:
        if not self.enabled or not self.token:
            return False
        min_post = self.cfg.get("discord", {}).get("alerts", {}).get("min_priority_to_post", 1)
        if priority < min_post:
            return False

        channel = self.channel_id
        if not channel:
            log.warning("No Discord channel configured")
            return False

        payload: dict = {"content": message[:2000]}
        if embed:
            payload["embeds"] = [embed]

        return self._curl(f"{self.BASE_URL}/channels/{channel}/messages", payload)

    def push_dm(self, message: str, priority: int = 2, embed: Optional[dict] = None) -> bool:
        if not self.enabled or not self.token or not self.user_id:
            return False
        min_dm = self.cfg.get("discord", {}).get("alerts", {}).get("min_priority_to_dm", 2)
        if priority < min_dm:
            return False

        # Create DM channel first
        dm_result = subprocess.run(
            ["curl", "-s", "-w", "%{http_code}", "-X", "POST",
             "-H", f"Authorization: Bot {self.token}",
             "-H", "Content-Type: application/json",
             "-d", json.dumps({"recipient_id": self.user_id}),
             "--connect-timeout", "5", "--max-time", "15",
             f"{self.BASE_URL}/users/@me/channels"],
            capture_output=True, text=True, timeout=20
        )

        stdout = dm_result.stdout
        body = stdout[:-3] if len(stdout) >= 3 else stdout
        try:
            dm_data = json.loads(body)
            dm_channel_id = dm_data.get("id", "")
        except (json.JSONDecodeError, Exception):
            log.warning("DM channel creation failed")
            return False

        if not dm_channel_id:
            return False

        payload = {"content": message[:2000]}
        if embed:
            payload["embeds"] = [embed]

        return self._curl(f"{self.BASE_URL}/channels/{dm_channel_id}/messages", payload)

    def get_embed_color(self, msg_type: str) -> int:
        cmap = self.cfg.get("discord", {}).get("formatting", {}).get("color_map", {})
        return cmap.get(msg_type, 0x3498db)
