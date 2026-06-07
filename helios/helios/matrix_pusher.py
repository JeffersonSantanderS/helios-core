"""Helios v6 — Matrix push via Client-Server API.

Replaces DiscordPusher. Supports HTML formatting via m.room.message
with formatted_body.  No external deps beyond stdlib + curl.

Configurable rooms (home channel + DM room).  Falls back to local
logging if token is missing.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import html

log = logging.getLogger("helios.matrix_push")

_DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
_DM_ROOM_FILE = _DATA_DIR / "matrix_dm_room.json"


class MatrixPusher:
    """Drop-in replacement for DiscordPusher."""

    def __init__(self, cfg: Optional[Any] = None):
        self.cfg = cfg or {}
        self.enabled = self._resolve_bool(self.cfg, "matrix.enabled", True)

        # Token discovery: env -> .env file -> fall-through
        self.token = self._resolve_str(self.cfg, "matrix.access_token", "")
        if not self.token:
            self.token = self._auto_detect_token()

        self.homeserver = self._resolve_str(
            self.cfg, "matrix.homeserver",
            os.environ.get("MATRIX_HOMESERVER", "")
        ).rstrip("/")

        self.home_room = self._resolve_str(
            self.cfg, "matrix.room",
            os.environ.get("MATRIX_HOME_ROOM", "")
        )

        self.dm_user = self._resolve_str(
            self.cfg, "matrix.dm_user",
            os.environ.get("MATRIX_ALLOWED_USERS", "")
        ).strip()
        # If the value lacks a domain, append the homeserver domain; otherwise preserve full MXID
        if self.dm_user:
            if not self.dm_user.startswith("@"):
                self.dm_user = "@" + self.dm_user
            if ":" not in self.dm_user:
                # Derive domain from homeserver URL
                from urllib.parse import urlparse
                parsed = urlparse(self.homeserver)
                domain = parsed.hostname or ""
                self.dm_user = self.dm_user + f":{domain}"

        self._last_push_ts: float = 0.0
        self._dm_room_cached: Optional[str] = None

        if not self.token:
            log.warning("MatrixPusher: no access token — deliveries disabled")
        if not self.home_room and self.enabled:
            log.warning("MatrixPusher: no home_room configured")

    # ── helpers ──────────────────────────────────────────────────────

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
        for env_var in ("MATRIX_ACCESS_TOKEN", "HELIOS_MATRIX_TOKEN"):
            tok = os.environ.get(env_var, "")
            if tok and tok.strip():
                log.info("Matrix token from $%s", env_var)
                return tok.strip()
        for p in (Path.home() / ".hermes" / ".env", Path.home() / ".hermes" / "config" / ".env"):
            if p.exists():
                try:
                    for line in p.read_text().splitlines():
                        line = line.strip()
                        if line.startswith("MATRIX_ACCESS_TOKEN="):
                            return line.split("=", 1)[1].strip().strip('"').strip("'")
                except Exception:
                    pass
        return ""

    def _curl(self, url: str, method: str = "POST", payload: Optional[dict] = None,
               timeout: int = 15) -> tuple[bool, str]:
        """Raw curl helper. Returns (ok, body)."""
        try:
            data = json.dumps(payload) if payload else "{}"
            result = subprocess.run(
                ["curl", "-s", "-S", "-w", "\n%{http_code}", "-X", method,
                 "-H", f"Authorization: Bearer {self.token}",
                 "-H", "Content-Type: application/json",
                 "-d", data,
                 "--connect-timeout", "5",
                 "--max-time", str(timeout),
                 f"{self.homeserver}{url}"],
                capture_output=True, text=True, timeout=timeout + 5
            )
            lines = result.stdout.splitlines()
            status = int(lines[-1]) if lines and lines[-1].isdigit() else 0
            body = "\n".join(lines[:-1]) if len(lines) > 1 else result.stdout
            return status in (200, 201, 202), body
        except Exception as exc:
            log.warning("Matrix curl failed: %s", exc)
            return False, ""

    # ── public API ───────────────────────────────────────────────────

    def push(self, message: str, priority: int = 0, embed: Optional[dict] = None) -> bool:
        """Post to the configured home room. Converts embed dict to HTML."""
        if not self.enabled or not self.token or not self.home_room:
            return False

        # Priority gating
        min_post = self.cfg.get("matrix", {}).get("alerts", {}).get("min_priority_to_post", 1)
        if priority < min_post:
            return False

        body_plain = message
        body_html = self._embed_to_html(embed) if embed else self._md_to_html(message)

        txn_id = f"helios_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{os.urandom(3).hex()}"
        payload = {
            "msgtype": "m.text",
            "body": body_plain,
        }
        if body_html:
            payload["format"] = "org.matrix.custom.html"
            payload["formatted_body"] = body_html

        ok, _ = self._curl(f"/_matrix/client/v3/rooms/{self.home_room}/send/m.room.message/{txn_id}",
                           method="PUT", payload=payload)
        if ok:
            log.info("Matrix push OK (room %s)", self.home_room)
        else:
            log.warning("Matrix push failed (room %s)", self.home_room)
        return ok

    def push_dm(self, message: str, priority: int = 2, embed: Optional[dict] = None) -> bool:
        """Post to a DM room with the configured user."""
        if not self.enabled or not self.token or not self.dm_user:
            return False

        # Priority gating
        min_dm = self.cfg.get("matrix", {}).get("alerts", {}).get("min_priority_to_dm", 2)
        if priority < min_dm:
            return False

        dm_room = self._ensure_dm_room()
        if not dm_room:
            # Fallback: post to home room with @mention
            if self.home_room:
                escaped = f"{self.dm_user} " + message
                return self.push(escaped, priority=priority, embed=embed)
            return False

        body_plain = message
        body_html = self._embed_to_html(embed) if embed else self._md_to_html(message)

        txn_id = f"helios_dm_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{os.urandom(3).hex()}"
        payload = {
            "msgtype": "m.text",
            "body": body_plain,
        }
        if body_html:
            payload["format"] = "org.matrix.custom.html"
            payload["formatted_body"] = body_html

        ok, _ = self._curl(f"/_matrix/client/v3/rooms/{dm_room}/send/m.room.message/{txn_id}",
                           method="PUT", payload=payload)
        if ok:
            log.info("Matrix DM push OK")
        else:
            log.warning("Matrix DM push failed")
        return ok

    def push_routed(self, message: str, priority: int, source: str = "") -> tuple:
        """Route by priority."""
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

    # ── internal: DM room management ──────────────────────────────────

    def _ensure_dm_room(self) -> Optional[str]:
        """Return existing DM room ID or create one."""
        if self._dm_room_cached:
            return self._dm_room_cached
        if _DM_ROOM_FILE.exists():
            try:
                data = json.loads(_DM_ROOM_FILE.read_text())
                rid = data.get("room_id")
                if rid:
                    self._dm_room_cached = rid
                    return rid
            except Exception:
                pass

        if not self.dm_user:
            return None

        ok, body = self._curl("/_matrix/client/v3/createRoom",
                              method="POST",
                              payload={
                                  "preset": "trusted_private_chat",
                                  "invite": [self.dm_user],
                                  "is_direct": True,
                                  "topic": "Helios DM",
                              })
        if ok:
            try:
                data = json.loads(body)
                rid = data.get("room_id")
                if rid:
                    self._dm_room_cached = rid
                    _DATA_DIR.mkdir(parents=True, exist_ok=True)
                    _DM_ROOM_FILE.write_text(json.dumps({"room_id": rid}))
                    return rid
            except Exception:
                pass
        return None

    # ── formatting helpers ─────────────────────────────────────────────

    @staticmethod
    def _md_to_html(text: str) -> str:
        """Simple Markdown → HTML for Matrix formatted_body.

        Escapes raw text before applying Markdown formatting.
        """
        import re
        # 1. Escape HTML special chars in the raw text
        text = html.escape(text)
        # 2. Markdown formatting (applied to escaped text — safe because
        #    the regex patterns won’t match entities like &lt;)
        text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'(?<![*\w])\*(.*?)\*(?![*\w])', r'<em>\1</em>', text)
        text = re.sub(r'`(.*?)`', r'<code>\1</code>', text)
        text = text.replace("\n", "<br>")
        return text

    @staticmethod
    def _embed_to_html(embed: dict) -> Optional[str]:
        """Convert an embed dict to Matrix HTML.

        Each text field is HTML-escaped before tag insertion.
        """
        if not embed:
            return None
        parts = []
        title = embed.get("title", "")
        if title:
            parts.append(f"<h3>{html.escape(title)}</h3>")
        desc = embed.get("description", "")
        if desc:
            parts.append(f"<p>{html.escape(desc).replace(chr(10), '<br>')}</p>")
        color = embed.get("color", "")
        if color and isinstance(color, int):
            parts.append(f"<!-- color: #{color:06x} -->")
        fields = embed.get("fields", [])
        if fields:
            parts.append("<ul>")
            for f in fields:
                name = html.escape(f.get('name', ''))
                value = html.escape(f.get('value', ''))
                parts.append(f"<li><strong>{name}:</strong> {value}</li>")
            parts.append("</ul>")
        footer = embed.get("footer", {}).get("text", "")
        if footer:
            parts.append(f"<p><small>{html.escape(footer)}</small></p>")
        return "".join(parts)

    def get_embed_color(self, msg_type: str) -> int:
        """Stub: embed colours don't apply to Matrix HTML, but keep API parity."""
        cmap = self.cfg.get("matrix", {}).get("formatting", {}).get("color_map", {})
        return cmap.get(msg_type, 0x3498db)
