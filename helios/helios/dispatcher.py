"""Helios v6 — Alert Dispatcher.

The "mouth" of Helios. Receives fired rules from the engine, applies
rate limiting and cooldown, formats messages for delivery, and sends
via ChannelRouter (primary) or MatrixPusher (fallback). Tracks alert
history and supports snoozing.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from .matrix_pusher import MatrixPusher
from .channels.router import ChannelRouter
from .channels.events import AlertEvent
from .channels.base import ChannelResult

log = logging.getLogger("helios.dispatcher")

# ── Severity / Embed Color Map ──────────────────────────────────────
SEVERITY_COLORS = {
    "info":     0x3498db,  # blue
    "warning":  0xf39c12,  # orange
    "critical": 0xe74c3c,  # red
    "success":  0x2ecc71,  # green
    "system":   0x9b59b6,  # purple
}

SEVERITY_ICONS = {
    "info":     "ℹ️",
    "warning":  "⚠️",
    "critical": "🚨",
    "success":  "✅",
    "system":   "⚙️",
}

CATEGORY_ICONS = {
    "server_health":  "🖥️",
    "data_freshness": "📡",
    "location":       "📍",
    "health":         "❤️",
    "behavioral":     "🧠",
    "system":         "⚙️",
    "scheduled":      "📅",
    "mac_bridge":     "💻",
    "anomaly":        "🔍",
    "protein":        "🥩",
    "nutrition":       "🍽️",
    "weather":        "🌤️",
    "gaming":         "🎮",
    "focus":          "💡",
    "calendar":       "📆",
}


class AlertDispatcher:
    """Rate-limited alert dispatch with history and snooze support."""

    def __init__(self, db, matrix_pusher: MatrixPusher, config: dict = None,
                 channels: Optional[ChannelRouter] = None):
        self.db = db
        self.matrix_pusher = matrix_pusher
        self.cfg = config or {}
        self.channels = channels
        self._hourly_counts: dict[str, list[float]] = {}  # slug -> list of timestamps
        self._snoozed: dict[str, float] = {}              # slug -> un-snooze timestamp
        self._hourly_total: list[float] = []              # global rate limiter

        # Configurable limits
        alerts_cfg = self.cfg.get("alerts", {})
        self.max_per_rule_per_hour = alerts_cfg.get("max_per_rule_per_hour", 2)
        self.max_total_per_hour = alerts_cfg.get("max_total_per_hour", 5)
        self.rate_window = alerts_cfg.get("rate_window_secs", 3600)

    # ── Public API ──────────────────────────────────────────────────

    def dispatch(self, hit: dict, context: dict) -> bool:
        """Dispatch a rule hit as an alert. Returns True if sent.

        ChannelRouter is the primary outbound path. MatrixPusher is the
        fallback when channels is None or fails. Preserves snooze, rate
        limiting, template rendering, and DB history.
        """
        slug = hit.get("slug", "unknown")
        severity = hit.get("severity", "info")
        category = hit.get("category", "system")

        # Check snooze
        if slug in self._snoozed and time.time() < self._snoozed[slug]:
            log.debug("Rule %s is snoozed until %s", slug,
                      datetime.fromtimestamp(self._snoozed[slug]).isoformat())
            return False

        # Rate limit check
        if not self._check_rate_limits(slug):
            log.debug("Rate limit hit for rule %s — suppressing", slug)
            return False

        # Format message + embed
        message, embed = self._format_alert(hit, context, severity, category)
        if message is None:
            # Template render failed — log and skip instead of sending raw template
            log.warning("Skipping dispatch for %s: template render failed", slug)
            self._log_alert(slug, severity, category, "template_render_failed", False, context)
            return False

        # Route by priority
        priority = hit.get("priority", 1)
        embed_safe = embed or {}
        sent = self._send_alert(message, embed_safe, priority, slug, severity, category)

        # Log to alert history
        self._log_alert(slug, severity, category, message, sent, context)
        self._record_rate(slug)

        if sent:
            log.info("Alert dispatched: %s [%s] → %s", slug, severity,
                     "DM" if priority >= 3 else "channel")

        return sent

    def snooze(self, slug: str, minutes: int) -> str:
        """Snooze a rule for N minutes. Returns confirmation message."""
        if minutes <= 0:
            # Un-snooze
            self._snoozed.pop(slug, None)
            return f"Unsnoozed rule `{slug}`"
        until = time.time() + minutes * 60
        self._snoozed[slug] = until
        until_str = datetime.fromtimestamp(until).strftime("%H:%M")
        return f"Snoozed rule `{slug}` until {until_str} ({minutes} min)"

    def get_active_snoozes(self) -> dict[str, int]:
        """Return {slug: minutes_remaining} for active snoozes."""
        now = time.time()
        return {
            slug: int((until - now) / 60)
            for slug, until in self._snoozed.items()
            if now < until
        }

    def get_recent_alerts(self, limit: int = 20) -> list[dict]:
        """Return recent alert history."""
        try:
            with self.db._conn() as c:
                rows = c.execute(
                    "SELECT * FROM alert_history ORDER BY ts DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_alert_stats(self, hours: int = 24) -> dict:
        """Return alert stats for the last N hours."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        try:
            with self.db._conn() as c:
                total = c.execute(
                    "SELECT COUNT(*) FROM alert_history WHERE ts >= ?", (cutoff,)
                ).fetchone()[0]
                by_sev = c.execute(
                    "SELECT severity, COUNT(*) FROM alert_history WHERE ts >= ? GROUP BY severity",
                    (cutoff,)
                ).fetchall()
                by_cat = c.execute(
                    "SELECT category, COUNT(*) FROM alert_history WHERE ts >= ? GROUP BY category",
                    (cutoff,)
                ).fetchall()
            return {
                "total": total,
                "by_severity": {r[0]: r[1] for r in by_sev},
                "by_category": {r[0]: r[1] for r in by_cat},
                "hours": hours,
            }
        except Exception:
            return {"total": 0, "by_severity": {}, "by_category": {}, "hours": hours}

    # ── Internal ────────────────────────────────────────────────────

    def _send_alert(self, message: str, embed: dict, priority: int,
                    slug: str, severity: str, category: str) -> bool:
        """Send an alert through ChannelRouter (primary) or MatrixPusher (fallback).

        Priority routing:
        - priority >= 3: DM (urgent)
        - priority >= 2: channel + DM (important — two messages)
        - priority < 2: channel only (routine)

        When ChannelRouter is available and succeeds, MatrixPusher is not called.
        When ChannelRouter fails or is unavailable, falls back to MatrixPusher.
        """
        if self.channels is not None:
            sent = self._send_via_channels(message, embed, priority, slug, severity, category)
            if sent:
                return True
            # ChannelRouter failed — fall back to direct MatrixPusher
            log.debug("ChannelRouter send failed for alert %s, falling back to MatrixPusher", slug)

        return self._send_legacy_matrix(message, embed, priority)

    def _send_via_channels(self, message: str, embed: dict, priority: int,
                           slug: str, severity: str, category: str) -> bool:
        """Try sending via ChannelRouter. Returns True if any channel succeeded.

        For priority >= 2 alerts, sends both a channel message and a DM
        (matching the existing dual-send behavior).
        """
        assert self.channels is not None  # guarded by _send_alert
        alert_event = AlertEvent(
            title=embed.get("title", slug),
            message=message,
            severity=severity,
            priority=priority,
            category=category,
            source="alert_dispatcher",
            embed=embed,
            slug=slug,
        )

        try:
            results = self.channels.send(alert_event)
        except Exception as exc:
            log.warning("ChannelRouter send failed for %s: %s", slug, exc)
            return False

        if not results:
            log.debug("ChannelRouter returned no results for %s", slug)
            return False

        sent = any(r.success for r in results)

        # For priority >= 2, also send a DM notification (dual-send)
        if sent and priority >= 2:
            dm_event = AlertEvent(
                title=f"🚨 {embed.get('title', slug)}",
                message=message,
                severity=severity,
                priority=3,  # DM-level priority
                category=category,
                source="alert_dispatcher",
                embed=embed,
                slug=f"{slug}_dm",
            )
            try:
                self.channels.send(dm_event)
            except Exception as exc:
                log.debug("ChannelRouter DM send failed for %s: %s", slug, exc)
                # DM failure is non-fatal — primary alert was sent

        return sent

    def _send_legacy_matrix(self, message: str, embed: dict, priority: int) -> bool:
        """Send via direct MatrixPusher (fallback path).

        This preserves the original priority routing logic.
        """
        if priority >= 3:
            return self.matrix_pusher.push_dm(message, priority=priority, embed=embed)
        elif priority >= 2:
            sent = self.matrix_pusher.push(message, priority=priority, embed=embed)
            if sent:
                self.matrix_pusher.push_dm(f"🚨 {message}", priority=3, embed=embed)
            return sent
        else:
            return self.matrix_pusher.push(message, priority=priority, embed=embed)

    def _check_rate_limits(self, slug: str) -> bool:
        """Check per-rule and global rate limits."""
        now = time.time()
        window_start = now - self.rate_window

        # Per-rule limit
        ts_list = self._hourly_counts.get(slug, [])
        ts_list = [t for t in ts_list if t > window_start]
        self._hourly_counts[slug] = ts_list
        if len(ts_list) >= self.max_per_rule_per_hour:
            return False

        # Global limit
        self._hourly_total = [t for t in self._hourly_total if t > window_start]
        if len(self._hourly_total) >= self.max_total_per_hour:
            return False

        return True

    def _record_rate(self, slug: str) -> None:
        now = time.time()
        self._hourly_counts.setdefault(slug, []).append(now)
        self._hourly_total.append(now)

    def _format_alert(self, hit: dict, context: dict, severity: str,
                       category: str) -> tuple[Optional[str], Optional[dict]]:
        """Build message and embed from a rule hit.

        Returns (msg, embed) on success. Returns (None, None) if template
        rendering fails — the caller must NOT send raw templates.
        """
        msg_template = hit.get("message", hit.get("description", "Helios alert"))
        # Build flat interpolation dict from context modules
        interpolations: dict[str, Any] = {}
        for mod_name, mod_vals in context.items():
            if isinstance(mod_vals, dict):
                for kk, vv in mod_vals.items():
                    if not kk.startswith("_"):
                        interpolations[f"{mod_name}.{kk}"] = vv
                        interpolations[kk] = vv

        # Replace {module.key} patterns manually — str.format() treats dots as
        # attribute access, not flat keys. Use re.sub to match and replace.
        import re as _re
        def _replace_template(m: _re.Match) -> str:
            key = m.group(1)
            val = interpolations.get(key)
            if val is None:
                raise KeyError(f"Missing template key: {key}")
            if isinstance(val, (int, float)):
                # Support format spec like :.0f
                spec = m.group(2) or ""
                if spec:
                    return f"{val:{spec}}"
                return str(val)
            return str(val)

        pattern = r"\{([a-zA-Z_][a-zA-Z0-9_.]*)(?::([^}]*))?\}"
        try:
            msg = _re.sub(pattern, _replace_template, msg_template)
        except KeyError as exc:
            log.warning("Alert template render failed for %s: %s",
                        hit.get("slug", "unknown"), exc)
            return None, None
        except Exception as exc:
            log.warning("Alert template render failed for %s: %s",
                        hit.get("slug", "unknown"), exc)
            return None, None

        cat_icon = CATEGORY_ICONS.get(category, "📌")
        sev_icon = SEVERITY_ICONS.get(severity, "ℹ️")
        color = SEVERITY_COLORS.get(severity, 0x3498db)

        title = f"{sev_icon} {cat_icon} {hit.get('title', category.replace('_', ' ').title())}"

        embed = {
            "title": title,
            "description": msg[:4096],
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {
                "text": f"Helios v6 · Rule: {hit['slug']}",
            },
        }

        rule_desc = hit.get("description", "")
        if rule_desc:
            embed["fields"] = [{
                "name": "Rule",
                "value": rule_desc[:1024],
                "inline": False,
            }]

        return msg, embed

    def _log_alert(self, slug: str, severity: str, category: str,
                    message: str, sent: bool, context: dict) -> None:
        """Write alert to alert_history table."""
        try:
            with self.db._conn() as c:
                c.execute(
                    """INSERT INTO alert_history (rule_slug, severity, category, message, sent, context)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (slug, severity, category, message[:2000],
                     1 if sent else 0, json.dumps(context or {}))
                )
                c.commit()
        except Exception as exc:
            log.debug("Failed to log alert: %s", exc)
