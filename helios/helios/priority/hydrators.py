"""Candidate hydrators — enrich candidates with context."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import Candidate
from .config import PriorityConfig


class CandidateHydrator:
    """Add user state, system state, alert history, and other context."""

    def __init__(self, db: Any, cfg: PriorityConfig, preferences: Any = None, health: Any = None):
        self.db = db
        self.cfg = cfg
        self.preferences = preferences
        self.health = health

    def hydrate(self, candidate: Candidate, context: dict[str, Any]) -> Candidate:
        """Enrich a single candidate with all available context."""
        hydrated: dict[str, Any] = {}

        # User state inference from current context
        hydrated["user_state"] = self._infer_user_state(context)

        # Alert history for this candidate
        hydrated["alert_history"] = self._get_alert_history(candidate)

        # Priority decision history for repeat/annoyance signals
        hydrated["priority_history"] = self._get_priority_history(candidate)

        # Home context if relevant
        if candidate.category == "home" or candidate.candidate_type == "home_environment_alert":
            hydrated["home"] = self._get_home_context(context)

        # Module health if relevant
        if candidate.module:
            hydrated["module_health"] = self._get_module_health(candidate.module)

        # Calendar / task context
        hydrated["calendar"] = self._get_calendar_context(context)
        hydrated["tasks"] = self._get_task_context(context)

        candidate.hydrated = hydrated
        return candidate

    # ── internal helpers ──────────────────────────────────────────────

    def _infer_user_state(self, context: dict[str, Any]) -> dict[str, Any]:
        """Infer user activity state from context."""
        state: dict[str, Any] = {
            "is_home": False,
            "is_away": False,
            "is_driving": False,
            "is_sleeping": False,
            "is_work_hours": False,
            "is_quiet_hours": False,
            "is_in_calendar_event": False,
            "is_focus_mode": False,
            "recent_mood": None,
            "recent_activity_level": None,
        }

        # Location
        loc = context.get("location", {})
        if isinstance(loc, dict):
            home_zone = loc.get("is_home")
            if home_zone is not None:
                state["is_home"] = bool(home_zone)
                state["is_away"] = not bool(home_zone)
            vehicle = loc.get("in_vehicle")
            if vehicle is not None:
                state["is_driving"] = bool(vehicle)

        # Work hours: M-F 07:00–17:00 MDT
        now = datetime.now(timezone.utc)
        mdt = now.astimezone(__import__("zoneinfo").ZoneInfo("America/Edmonton"))
        weekday = mdt.weekday()
        hour = mdt.hour
        state["is_work_hours"] = weekday < 5 and 7 <= hour < 17

        # Quiet hours: 22:00–07:00
        state["is_quiet_hours"] = hour >= 22 or hour < 7

        # Sleep proxy: quiet hours + at home
        state["is_sleeping"] = state["is_quiet_hours"] and state["is_home"]

        # Calendar
        cal = context.get("calendar", {})
        if isinstance(cal, dict):
            events_today = cal.get("events_today", 0)
            next_event_minutes = cal.get("next_event_minutes", None)
            state["is_in_calendar_event"] = (
                next_event_minutes is not None and next_event_minutes <= 0
            )

        # Mood
        mood = context.get("mood", {})
        if isinstance(mood, dict):
            state["recent_mood"] = mood.get("last_mood")

        # Activity / focus from idle or active window
        idle = context.get("idle", {})
        if isinstance(idle, dict):
            active_seconds = idle.get("active_seconds", 0)
            if active_seconds > 300:
                state["recent_activity_level"] = "active"
            elif active_seconds > 60:
                state["recent_activity_level"] = "light"
            else:
                state["recent_activity_level"] = "idle"

        return state

    def _get_alert_history(self, candidate: Candidate) -> dict[str, Any]:
        """Pull recent alert history for similar items."""
        history: dict[str, Any] = {
            "same_rule_sent_1h": 0,
            "same_rule_sent_24h": 0,
            "category_sent_1h": 0,
            "category_sent_24h": 0,
            "last_sent_at": None,
        }
        try:
            # Query alert_history table via DB
            if self.db and candidate.rule_slug:
                rows_1h = self.db._execute(
                    """SELECT COUNT(*) FROM alert_history
                       WHERE rule_slug = ? AND ts > datetime('now', '-1 hour')""",
                    (candidate.rule_slug,),
                ).fetchone()
                history["same_rule_sent_1h"] = rows_1h[0] if rows_1h else 0

                rows_24h = self.db._execute(
                    """SELECT COUNT(*) FROM alert_history
                       WHERE rule_slug = ? AND ts > datetime('now', '-24 hour')""",
                    (candidate.rule_slug,),
                ).fetchone()
                history["same_rule_sent_24h"] = rows_24h[0] if rows_24h else 0

                rows_cat_1h = self.db._execute(
                    """SELECT COUNT(*) FROM alert_history
                       WHERE category = ? AND ts > datetime('now', '-1 hour')""",
                    (candidate.category,),
                ).fetchone()
                history["category_sent_1h"] = rows_cat_1h[0] if rows_cat_1h else 0

                rows_cat_24h = self.db._execute(
                    """SELECT COUNT(*) FROM alert_history
                       WHERE category = ? AND ts > datetime('now', '-24 hour')""",
                    (candidate.category,),
                ).fetchone()
                history["category_sent_24h"] = rows_cat_24h[0] if rows_cat_24h else 0

                last = self.db._execute(
                    """SELECT ts FROM alert_history
                       WHERE rule_slug = ? ORDER BY ts DESC LIMIT 1""",
                    (candidate.rule_slug,),
                ).fetchone()
                history["last_sent_at"] = last[0] if last else None
        except Exception:
            pass
        return history

    def _get_home_context(self, context: dict[str, Any]) -> dict[str, Any]:
        home = context.get("home", {})
        if not isinstance(home, dict):
            return {}
        return {
            "rooms_occupied": home.get("rooms_occupied"),
            "total_lights_on": home.get("total_lights_on"),
            "anyone_home": home.get("anyone_home", False),
            "master_bedroom_occupied": home.get("master_bedroom_occupied"),
            "master_bedroom_temp_c": home.get("master_bedroom_temp_c"),
            "master_bedroom_lux": home.get("master_bedroom_lux"),
            "spare_bedroom_occupied": home.get("spare_bedroom_occupied"),
            "spare_bedroom_temp_c": home.get("spare_bedroom_temp_c"),
        }

    def _get_module_health(self, module: str | None) -> dict[str, Any]:
        if not module or not self.health:
            return {"status": "unknown", "failures": 0}
        try:
            if hasattr(self.health, "summary"):
                summary = self.health.summary()
                if isinstance(summary, dict) and module in summary:
                    return summary[module]
            if hasattr(self.health, "get_health"):
                return self.health.get_health(module)
            return {"status": "unknown", "failures": 0}
        except Exception:
            return {"status": "unknown", "failures": 0}

    def _get_priority_history(self, candidate: Candidate) -> dict[str, Any]:
        """Query priority_decisions table for repeat/annoyance signals.

        Uses fingerprint when available for stable cross-tick repeat detection,
        falling back to candidate_id for candidates without fingerprints.
        """
        history = {
            "same_candidate_1h": 0,
            "same_candidate_24h": 0,
            "same_fingerprint_1h": 0,
            "same_fingerprint_24h": 0,
            "same_title_1h": 0,
            "same_category_1h": 0,
            "recent_suppressions": 0,
            "recent_selections": 0,
            "last_decision": None,
            "last_decision_ts": None,
        }
        try:
            if not self.db:
                return history

            # Stable repeat detection via fingerprint
            fp = candidate.fingerprint
            if fp:
                rows = self.db._execute(
                    """SELECT COUNT(*) FROM priority_decisions
                       WHERE candidate_id IN (
                           SELECT candidate_id FROM priority_candidates
                           WHERE fingerprint = ?
                       ) AND ts > datetime('now', '-1 hour')""",
                    (fp,),
                ).fetchone()
                history["same_fingerprint_1h"] = rows[0] if rows else 0

                rows = self.db._execute(
                    """SELECT COUNT(*) FROM priority_decisions
                       WHERE candidate_id IN (
                           SELECT candidate_id FROM priority_candidates
                           WHERE fingerprint = ?
                       ) AND ts > datetime('now', '-24 hour')""",
                    (fp,),
                ).fetchone()
                history["same_fingerprint_24h"] = rows[0] if rows else 0

                # Last decision for this fingerprint
                last = self.db._execute(
                    """SELECT decision, ts FROM priority_decisions
                       WHERE candidate_id IN (
                           SELECT candidate_id FROM priority_candidates
                           WHERE fingerprint = ?
                       ) ORDER BY ts DESC LIMIT 1""",
                    (fp,),
                ).fetchone()
                if last:
                    history["last_decision"] = last[0]
                    history["last_decision_ts"] = last[1]

            # Fallback: same candidate_id (less useful with UUID-based IDs)
            if candidate.candidate_id and not fp:
                rows = self.db._execute(
                    """SELECT COUNT(*) FROM priority_decisions
                       WHERE candidate_id = ? AND ts > datetime('now', '-1 hour')""",
                    (candidate.candidate_id,),
                ).fetchone()
                history["same_candidate_1h"] = rows[0] if rows else 0

                rows = self.db._execute(
                    """SELECT COUNT(*) FROM priority_decisions
                       WHERE candidate_id = ? AND ts > datetime('now', '-24 hour')""",
                    (candidate.candidate_id,),
                ).fetchone()
                history["same_candidate_24h"] = rows[0] if rows else 0

                last = self.db._execute(
                    """SELECT decision, ts FROM priority_decisions
                       WHERE candidate_id = ? ORDER BY ts DESC LIMIT 1""",
                    (candidate.candidate_id,),
                ).fetchone()
                if last:
                    history["last_decision"] = last[0]
                    history["last_decision_ts"] = last[1]

            # By title (loose match for home sensor candidates without rule_slug)
            title = candidate.title
            if title:
                rows = self.db._execute(
                    """SELECT COUNT(*) FROM priority_decisions
                       WHERE reason LIKE ? AND ts > datetime('now', '-1 hour')""",
                    (f"%{title}%",),
                ).fetchone()
                history["same_title_1h"] = rows[0] if rows else 0

            # Same category 1h
            rows = self.db._execute(
                """SELECT COUNT(*) FROM priority_decisions
                   WHERE (SELECT category FROM priority_candidates
                          WHERE priority_candidates.candidate_id = priority_decisions.candidate_id) = ?
                   AND ts > datetime('now', '-1 hour')""",
                (candidate.category,),
            ).fetchone()
            history["same_category_1h"] = rows[0] if rows else 0

            # Recent suppressions (24h)
            rows = self.db._execute(
                """SELECT COUNT(*) FROM priority_decisions
                   WHERE decision LIKE 'suppress_%' AND ts > datetime('now', '-24 hour')""",
            ).fetchone()
            history["recent_suppressions"] = rows[0] if rows else 0

            # Recent selections (24h)
            rows = self.db._execute(
                """SELECT COUNT(*) FROM priority_decisions
                   WHERE decision LIKE 'select_%' AND ts > datetime('now', '-24 hour')""",
            ).fetchone()
            history["recent_selections"] = rows[0] if rows else 0

        except Exception:
            pass
        return history

    def _get_calendar_context(self, context: dict[str, Any]) -> dict[str, Any]:
        cal = context.get("calendar", {})
        if not isinstance(cal, dict):
            return {}
        return {
            "events_today": cal.get("events_today", 0),
            "next_event_minutes": cal.get("next_event_minutes", None),
            "has_conflict": cal.get("has_conflict", False),
        }

    def _get_task_context(self, context: dict[str, Any]) -> dict[str, Any]:
        tasks = context.get("tasks", {})
        if not isinstance(tasks, dict):
            return {}
        return {
            "pending": tasks.get("pending", 0),
            "overdue": tasks.get("overdue", 0),
            "upcoming": tasks.get("upcoming", 0),
        }
