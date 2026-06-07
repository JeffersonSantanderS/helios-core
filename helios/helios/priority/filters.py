"""Candidate filters — hard and soft filtering."""

from __future__ import annotations

from typing import Any

from .models import Candidate
from .config import PriorityConfig, SafeguardConfig


class CandidateFilters:
    """Apply hard and soft filters to candidates."""

    CRITICAL_SEVERITIES = {"critical", "error"}

    def __init__(self, cfg: PriorityConfig):
        self.cfg = cfg

    def apply(self, candidates: list[Candidate], context: dict[str, Any]) -> list[Candidate]:
        """Return only candidates that pass all hard filters."""
        result: list[Candidate] = []
        seen_ids: set[str] = set()
        for c in candidates:
            ok, _ = self._hard_filter(c, seen_ids)
            if ok:
                seen_ids.add(c.candidate_id)
                result.append(c)
            else:
                c.status = "filtered"
        return result

    def _hard_filter(self, c: Candidate, seen_ids: set[str]) -> tuple[bool, str]:
        # Duplicate candidate_id
        if c.candidate_id in seen_ids:
            return False, "duplicate_candidate_id"

        # Malformed: missing title for notification-type candidate
        if not c.title.strip() and c.candidate_type in (
            "rule_alert",
            "home_environment_alert",
            "health_alert",
            "predictive_alert",
        ):
            return False, "missing_title"

        # Disabled source
        if c.source == "rules_v2" and not self.cfg.sources.rules:
            return False, "source_disabled"
        if c.source == "home" and not self.cfg.sources.home:
            return False, "source_disabled"
        if c.source == "module_health" and not self.cfg.sources.module_health:
            return False, "source_disabled"

        # Action config failure: action_config must be dict
        if c.action_config and not isinstance(c.action_config, dict):
            return False, "malformed_action_config"

        # Snoozed (placeholder — would check snooze table)
        # Currently not implemented in Phase 1

        return True, ""

    def annotate_soft(self, c: Candidate, context: dict[str, Any]) -> dict[str, Any]:
        """Return penalty annotations for the scorer, without removing candidate."""
        penalties: dict[str, Any] = {}

        # Quiet hours penalty
        user_state = c.hydrated.get("user_state", {})
        if user_state.get("is_quiet_hours"):
            penalties["quiet_hours"] = self.cfg.safeguards.quiet_hours_penalty

        # Driving penalty
        if user_state.get("is_driving"):
            penalties["driving"] = self.cfg.safeguards.driving_penalty

        # Meeting penalty
        if user_state.get("is_in_calendar_event"):
            penalties["meeting"] = self.cfg.safeguards.meeting_penalty

        return penalties

    def is_critical(self, c: Candidate) -> bool:
        return c.severity in self.CRITICAL_SEVERITIES

    def is_user_requested(self, c: Candidate) -> bool:
        """Heuristic: user-requested reminders have specific types or tags."""
        return "user_requested" in c.tags or c.candidate_type == "task_reminder"
