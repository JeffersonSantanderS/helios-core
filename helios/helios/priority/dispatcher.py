"""Priority Engine dispatch — route selected candidates to channels/DMs/summaries.

This is Phase 5: Priority Engine becomes the main routing brain for alerts.
Instead of dispatching rule hits directly, the engine dispatches selected
priority candidates based on their decision/route.

Phase 3 (channel adapters): PriorityDispatcher now mirrors dispatched events
to the ChannelRouter as AlertEvent objects, in addition to existing Matrix
delivery. The `channels` parameter is optional — if None, no channel mirroring
happens (backward compatible).
"""

from __future__ import annotations

import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..matrix_pusher import MatrixPusher
from ..dispatcher import AlertDispatcher
from ..channels.events import AlertEvent
from ..channels.router import ChannelRouter
from .models import PriorityResult, CandidateDecision, Candidate

log = logging.getLogger("helios.priority.dispatch")

# Summary queue persists to disk for recovery between ticks
_SUMMARY_QUEUE_PATH = Path.home() / ".hermes" / "helios" / "data" / "priority_engine" / "summary_queue.jsonl"


class PriorityDispatcher:
    """Send priority-selected candidates to the right channel surface.

    Phase 7: ChannelRouter is now the primary delivery path when available.
    When self.channels is present and not in shadow mode, AlertEvent is sent
    through the router (which routes to MatrixChannel for delivery). This
    replaces the direct self.matrix_pusher.push() calls, avoiding duplicate sends.

    When self.channels is None or fails, falls back to self.matrix_pusher.
    Shadow mode: ChannelRouter handles suppression internally.
    """

    def __init__(
        self,
        matrix_pusher: MatrixPusher,
        alert_dispatcher: AlertDispatcher | None = None,
        channels: Optional[ChannelRouter] = None,
    ):
        self.matrix_pusher = matrix_pusher
        self.alert_dispatcher = alert_dispatcher
        self.channels = channels
        self._summary_queue: list[dict[str, Any]] = []

    def _persist_summary_item(self, item: dict[str, Any]) -> None:
        """Append one summary-queue item to disk JSONL."""
        try:
            _SUMMARY_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _SUMMARY_QUEUE_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, default=str) + "\n")
        except Exception as exc:
            log.warning("Summary queue persist failed: %s", exc)

    def dispatch_result(self, result: PriorityResult, context: dict[str, Any], shadow_mode: bool = False) -> list[dict[str, Any]]:
        """Dispatch all selected candidates from a PriorityResult.

        Returns list of dispatch records for logging/feedback.
        If shadow_mode=True, queue/persist but do not actually send Matrix messages.
        """
        dispatched: list[dict[str, Any]] = []
        if not result.decisions:
            return dispatched

        # Build candidate lookup
        cand_map = {c.candidate_id: c for c in result.candidates}

        for dec in result.decisions:
            if not dec.decision.startswith("select_"):
                continue  # only dispatch selected candidates

            cand = cand_map.get(dec.candidate_id)
            if cand is None:
                continue

            record = self._dispatch_one(cand, dec, context, shadow_mode=shadow_mode)
            if record:
                dispatched.append(record)

        return dispatched

    def _dispatch_one(
        self,
        cand: Candidate,
        dec: CandidateDecision,
        context: dict[str, Any],
        shadow_mode: bool = False,
    ) -> dict[str, Any] | None:
        """Route a single selected candidate based on its decision/route.

        Phase 7: When ChannelRouter is available, route through it as primary.
        Falls back to direct MatrixPusher when channels is None or fails.
        No duplicate sends — if channels succeeds, matrix_pusher is skipped.
        """

        title = cand.title or "Priority Alert"
        message = cand.message or title
        severity = cand.severity or "info"
        category = cand.category or "system"

        # Build a compact embed
        embed = {
            "title": f"{self._severity_emoji(severity)} {title}",
            "description": message,
            "color": self._severity_color(severity),
            "footer": {
                "text": f"Score: {dec.final_score:.2f} | {cand.source} | {dec.reason[:60]}",
            },
        }

        # Route
        route = dec.route or "log"
        sent = False

        # Build AlertEvent for channel routing
        alert_event = AlertEvent(
            title=title,
            message=message,
            severity=severity,
            priority=cand.priority_hint or 1,
            category=category,
            source=cand.source or "priority_engine",
            embed=embed,
            slug=getattr(cand, "rule_slug", "") or "",
        )

        # Route-based dispatch
        if route == "matrix_dm":
            # Phase 7: Try ChannelRouter first, fall back to MatrixPusher
            if self.channels is not None:
                try:
                    results = self.channels.send(alert_event)
                    if any(r.success for r in results):
                        sent = True
                        log.debug("Priority DM dispatch via channel router: %s", cand.candidate_id)
                except Exception as exc:
                    log.warning("Channel router DM send failed for %s: %s, falling back",
                                cand.candidate_id, exc)
            if not sent:
                if not shadow_mode:
                    try:
                        sent = self.matrix_pusher.push_dm(message, priority=cand.priority_hint, embed=embed)
                    except Exception as exc:
                        log.warning("Priority DM dispatch failed: %s", exc)
                else:
                    sent = True  # track in shadow mode

        elif route == "matrix_channel":
            # Phase 7: Try ChannelRouter first, fall back to MatrixPusher
            if self.channels is not None:
                try:
                    results = self.channels.send(alert_event)
                    if any(r.success for r in results):
                        sent = True
                        log.debug("Priority channel dispatch via channel router: %s", cand.candidate_id)
                except Exception as exc:
                    log.warning("Channel router channel send failed for %s: %s, falling back",
                                cand.candidate_id, exc)
            if not sent:
                if not shadow_mode:
                    try:
                        sent = self.matrix_pusher.push(message, priority=cand.priority_hint, embed=embed)
                    except Exception as exc:
                        log.warning("Priority channel dispatch failed: %s", exc)
                else:
                    sent = True  # track in shadow mode

        elif route == "summary":
            # Queue for next summarizer run; persist to disk immediately
            summary_item = {
                "candidate_id": cand.candidate_id,
                "fingerprint": cand.fingerprint,
                "title": title,
                "message": message,
                "severity": severity,
                "category": category,
                "score": dec.final_score,
                "reason": dec.reason,
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }
            self._summary_queue.append(summary_item)
            self._persist_summary_item(summary_item)
            log.debug("Priority summary candidate queued: %s", cand.candidate_id)
            sent = True

        elif route == "log":
            # Log-only: no Matrix action
            log.debug("Priority log-only candidate: %s", cand.candidate_id)
            sent = True

        # LogChannel audit: send for summary/log routes that didn't go through channels
        if self.channels is not None and route in ("summary", "log"):
            try:
                self.channels.send(alert_event)
            except Exception as exc:
                log.debug("Channel audit for priority dispatch failed: %s", exc)

        return {
            "candidate_id": cand.candidate_id,
            "title": title,
            "route": route,
            "sent": sent,
            "score": dec.final_score,
            "reason": dec.reason,
        }

    @staticmethod
    def _severity_color(severity: str) -> int:
        return {
            "critical": 0xE74C3C,
            "error": 0xE67E22,
            "warning": 0xF1C40F,
            "info": 0x3498DB,
            "debug": 0x95A5A6,
        }.get(severity, 0x3498DB)

    @staticmethod
    def _severity_emoji(severity: str) -> str:
        return {
            "critical": "🚨",
            "error": "⚠️",
            "warning": "🔶",
            "info": "ℹ️",
            "debug": "🔍",
        }.get(severity, "ℹ️")
