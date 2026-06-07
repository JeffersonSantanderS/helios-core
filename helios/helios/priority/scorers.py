"""Candidate scorer — deterministic weighted scoring."""

from __future__ import annotations

from typing import Any

from .models import Candidate, CandidateScore
from .config import PriorityConfig
from .filters import CandidateFilters


class CandidateScorer:
    """Score a candidate along 12 dimensions using configurable weights."""

    SEVERITY_MAP = {
        "critical": 1.0,
        "error": 0.8,
        "warning": 0.6,
        "info": 0.2,
        "debug": 0.0,
    }

    CATEGORY_IMPORTANCE = {
        "health": 0.95,
        "home": 0.60,
        "system": 0.70,
        "security": 0.90,
        "calendar": 0.50,
        "tasks": 0.40,
        "spotify": 0.15,
        "mood": 0.30,
        "general": 0.20,
    }

    def __init__(self, cfg: PriorityConfig):
        self.cfg = cfg
        self.filters = CandidateFilters(cfg)

    def score(self, candidate: Candidate, context: dict[str, Any]) -> CandidateScore:
        """Compute full score for a candidate."""
        weights = self.cfg.weights

        # ── base dimensions ───────────────────────────────────────────
        urgency = self._score_urgency(candidate, context)
        importance = self._score_importance(candidate, context)
        relevance = self._score_relevance(candidate, context)
        confidence = self._score_confidence(candidate, context)
        context_fit = self._score_context_fit(candidate, context)
        actionability = self._score_actionability(candidate, context)
        novelty = self._score_novelty(candidate, context)
        safety = self._score_safety(candidate, context)
        disruption_cost = self._score_disruption(candidate, context)
        staleness = self._score_staleness(candidate, context)
        annoyance = self._score_annoyance(candidate, context)
        redundancy = self._score_redundancy(candidate, context)

        # ── soft filter penalties ────────────────────────────────────
        penalties = self.filters.annotate_soft(candidate, context)
        for key, penalty in penalties.items():
            if key == "quiet_hours":
                disruption_cost = min(1.0, disruption_cost + penalty)
                urgency = max(0.0, urgency - penalty * 0.5)
            elif key == "driving":
                disruption_cost = min(1.0, disruption_cost + penalty)
                relevance = max(0.0, relevance - penalty * 0.5)
            elif key == "meeting":
                disruption_cost = min(1.0, disruption_cost + penalty)
                relevance = max(0.0, relevance - penalty * 0.4)

        # ── weighted final score ─────────────────────────────────────
        raw = (
            weights.urgency * urgency
            + weights.importance * importance
            + weights.relevance * relevance
            + weights.confidence * confidence
            + weights.context_fit * context_fit
            + weights.actionability * actionability
            + weights.novelty * novelty
            + weights.safety * safety
            - weights.disruption_cost * disruption_cost
            - weights.staleness * staleness
            - weights.annoyance * annoyance
            - weights.redundancy * redundancy
        )

        # Normalize to 0–1 range using sigmoid-like clamping
        final = max(0.0, min(1.0, raw / 8.0))

        explanation = self._build_explanation(
            candidate, urgency, importance, relevance, confidence,
            context_fit, actionability, novelty, safety,
            disruption_cost, staleness, annoyance, redundancy,
            final, penalties,
        )

        factors = {
            "severity": candidate.severity,
            "priority_hint": candidate.priority_hint,
            "soft_penalties": penalties,
        }

        return CandidateScore(
            candidate_id=candidate.candidate_id,
            urgency=round(urgency, 3),
            importance=round(importance, 3),
            relevance=round(relevance, 3),
            confidence=round(confidence, 3),
            context_fit=round(context_fit, 3),
            actionability=round(actionability, 3),
            novelty=round(novelty, 3),
            safety=round(safety, 3),
            disruption_cost=round(disruption_cost, 3),
            staleness=round(staleness, 3),
            annoyance=round(annoyance, 3),
            redundancy=round(redundancy, 3),
            final_score=round(final, 3),
            explanation=explanation,
            factors=factors,
        )

    # ── individual dimension scorers ─────────────────────────────────

    def _score_urgency(self, c: Candidate, ctx: dict[str, Any]) -> float:
        base = self.SEVERITY_MAP.get(c.severity, 0.2)
        # Boost if rule priority hint is high
        if c.priority_hint >= 3:
            base = min(1.0, base + 0.2)
        # Boost if time-sensitive context clues
        home = c.hydrated.get("home", {})
        if c.category == "home" and home.get("master_bedroom_occupied"):
            base = min(1.0, base + 0.15)
        return base

    def _score_importance(self, c: Candidate, ctx: dict[str, Any]) -> float:
        base = self.CATEGORY_IMPORTANCE.get(c.category, 0.2)
        # Safety / health always high
        if c.severity == "critical":
            base = min(1.0, base + 0.3)
        # Rule priority
        base = min(1.0, base + (c.priority_hint - 1) * 0.1)
        return base

    def _score_relevance(self, c: Candidate, ctx: dict[str, Any]) -> float:
        user = c.hydrated.get("user_state", {})
        if not user:
            return 0.50  # neutral if unknown

        # Home alerts matter more when home
        if c.category == "home" and user.get("is_home"):
            return 0.90
        if c.category == "home" and user.get("is_away"):
            return 0.25

        # Health alerts matter during awake hours
        if c.category == "health" and not user.get("is_sleeping"):
            return 0.85

        # Calendar when not in meeting
        if c.category == "calendar" and not user.get("is_in_calendar_event"):
            return 0.70

        return 0.55

    def _score_confidence(self, c: Candidate, ctx: dict[str, Any]) -> float:
        base = 0.70
        # Fresh module health boosts confidence
        mh = c.hydrated.get("module_health", {})
        if mh.get("status") == "healthy":
            base = min(1.0, base + 0.15)
        elif mh.get("status") == "unhealthy":
            base = max(0.0, base - 0.30)
        elif mh.get("status") == "unknown":
            base = max(0.0, base - 0.15)

        # Sensor confidence for home module
        if c.category == "home":
            home_ctx = c.hydrated.get("home", {})
            if home_ctx.get("master_bedroom_temp_c") is not None:
                base = min(1.0, base + 0.10)

        # Deterministic rule hits have high confidence
        if c.source == "rules_v2":
            base = min(1.0, base + 0.10)

        return base

    def _score_context_fit(self, c: Candidate, ctx: dict[str, Any]) -> float:
        user = c.hydrated.get("user_state", {})
        if not user:
            return 0.50

        # Quiet hours → lower fit for non-critical
        if user.get("is_quiet_hours") and c.severity not in ("critical", "error"):
            return 0.20

        # Work hours → good fit for work-safe alerts
        if user.get("is_work_hours") and c.category in ("calendar", "tasks", "system"):
            return 0.75

        # Morning → good for briefings
        hour = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).astimezone(
            __import__("zoneinfo").ZoneInfo("America/Edmonton")
        ).hour
        if 6 <= hour <= 10 and c.candidate_type == "daily_briefing_item":
            return 0.85

        # Evening → good for summaries
        if 18 <= hour <= 22 and c.candidate_type == "summary":
            return 0.75

        return 0.60

    def _score_actionability(self, c: Candidate, ctx: dict[str, Any]) -> float:
        # High if clear action config
        if c.action_config and isinstance(c.action_config, dict):
            action = c.action_config.get("action", "")
            if action in ("push_routed", "matrix_push", "dm"):
                return 0.80
            if action == "log":
                return 0.30

        # Home alerts about lights are actionable
        if c.category == "home" and "light" in c.title.lower():
            return 0.70
        if c.category == "home" and "temperature" in c.title.lower():
            return 0.60

        # Calendar events are actionable
        if c.category == "calendar":
            return 0.65

        return 0.45

    def _score_novelty(self, c: Candidate, ctx: dict[str, Any]) -> float:
        hist = c.hydrated.get("alert_history", {})
        same_1h = hist.get("same_rule_sent_1h", 0)
        same_24h = hist.get("same_rule_sent_24h", 0)
        cat_1h = hist.get("category_sent_1h", 0)

        if same_1h > 0:
            return 0.05
        if same_24h >= 3:
            return 0.15
        if cat_1h > 0:
            return 0.40
        if same_24h > 0:
            return 0.60

        return 0.85

    def _score_safety(self, c: Candidate, ctx: dict[str, Any]) -> float:
        base = 0.30
        if c.category in ("health", "security"):
            base = 0.90
        if c.severity == "critical":
            base = 1.0
        if "safety" in c.title.lower() or "risk" in c.title.lower():
            base = min(1.0, base + 0.2)
        return base

    def _score_disruption(self, c: Candidate, ctx: dict[str, Any]) -> float:
        user = c.hydrated.get("user_state", {})
        cost = 0.20

        if user.get("is_sleeping"):
            cost = 0.90
        elif user.get("is_driving"):
            cost = 0.85
        elif user.get("is_in_calendar_event"):
            cost = 0.70
        elif user.get("is_quiet_hours"):
            cost = 0.60

        # DM is more disruptive than channel
        if c.candidate_type in ("dm", "llm_request"):
            cost = min(1.0, cost + 0.15)

        # Low-severity comfort nudges are disruptive
        if c.severity == "info" and c.category in ("home", "mood"):
            cost = min(1.0, cost + 0.1)

        return cost

    def _score_staleness(self, c: Candidate, ctx: dict[str, Any]) -> float:
        home = c.hydrated.get("home", {})
        mh = c.hydrated.get("module_health", {})
        score = 0.05

        # Sensor data missing
        if c.category == "home":
            temp = home.get("master_bedroom_temp_c")
            if temp is None:
                score = max(score, 0.60)
            # HA source freshness
            source = home.get("source")
            if source is None or source == "unavailable":
                score = max(score, 0.90)

        # Module freshness from health tracker
        if c.source == "module_health" or c.module:
            freshness = mh.get("freshness_secs")
            # Per-module overrides: batch sources (health, location) sync
            # on daily cadences, not real-time.  Use module-provided
            # thresholds so we don't penalise normal batch intervals.
            override = mh.get("_freshness_threshold_override")
            if isinstance(override, dict):
                stale_limit = override.get("stale", 900)
                degraded_limit = override.get("degraded", 3600)
            else:
                stale_limit = 900
                degraded_limit = 3600
            if freshness is not None:
                if freshness > degraded_limit:
                    score = max(score, 0.80)
                elif freshness > stale_limit:
                    score = max(score, 0.40)
            state = mh.get("state")
            if state == "stale":
                score = max(score, 0.60)
            elif state == "degraded":
                score = max(score, 0.40)

        return score

    def _score_annoyance(self, c: Candidate, ctx: dict[str, Any]) -> float:
        # Combine alert_history + priority_history for annoyance
        ah = c.hydrated.get("alert_history", {})
        ph = c.hydrated.get("priority_history", {})

        score = 0.0
        same_24h = ah.get("same_rule_sent_24h", 0)
        cat_24h = ah.get("category_sent_24h", 0)
        same_title_1h = ph.get("same_title_1h", 0)
        same_cat_1h = ph.get("same_category_1h", 0)

        if same_24h >= 5:
            score += 0.60
        elif same_24h >= 3:
            score += 0.35
        elif same_24h > 0:
            score += 0.10

        if cat_24h >= 10:
            score = min(1.0, score + 0.40)
        elif cat_24h >= 5:
            score = min(1.0, score + 0.20)

        # Same title seen recently from priority decisions also counts
        if same_title_1h > 1:
            score = min(1.0, score + 0.25)

        return score

    def _score_redundancy(self, c: Candidate, ctx: dict[str, Any]) -> float:
        # Cross-candidate redundancy is computed at selector level.
        # Per-candidate: if multiple alerts for same room in same tick
        home_tags = [t for t in c.tags if t in ("master_bedroom", "spare_bedroom", "living_room")]
        if len(home_tags) > 0:
            # Check if other candidates in this tick have the same room tag
            # This is a per-candidate heuristic; precise redundancy is tick-level in selector
            if c.candidate_type == "home_environment_alert" and len(home_tags) > 0:
                return 0.10  # slight redundancy bump for home sensor alerts
        return 0.0

    def _build_explanation(
        self, c: Candidate,
        urgency: float, importance: float, relevance: float,
        confidence: float, context_fit: float, actionability: float,
        novelty: float, safety: float, disruption_cost: float,
        staleness: float, annoyance: float, redundancy: float,
        final: float, penalties: dict[str, float],
    ) -> str:
        """Build human-readable explanation of why this candidate scored this way."""
        parts: list[str] = []
        factors: list[tuple[str, float]] = [
            ("urgency", urgency), ("importance", importance), ("relevance", relevance),
            ("confidence", confidence), ("context_fit", context_fit),
            ("actionability", actionability), ("novelty", novelty), ("safety", safety),
        ]
        factors.sort(key=lambda x: x[1], reverse=True)
        top_factors = [f"{name}={val:.2f}" for name, val in factors[:3]]
        parts.append(f"top factors: {', '.join(top_factors)}")

        if penalties:
            penalty_strs = [f"{k}({v:.2f})" for k, v in penalties.items()]
            parts.append(f"penalties: {', '.join(penalty_strs)}")

        if final >= 0.80:
            parts.append("score: very high")
        elif final >= 0.60:
            parts.append("score: high")
        elif final >= 0.40:
            parts.append("score: moderate")
        else:
            parts.append("score: low")

        if safety >= 0.8:
            parts.append("safety-critical")
        if disruption_cost >= 0.7:
            parts.append("high disruption risk")
        if staleness >= 0.5:
            parts.append("stale signal")
        if annoyance >= 0.4:
            parts.append("risk of annoyance")
        if redundancy >= 0.2:
            parts.append("redundant/duplicate")
        if novelty < 0.3:
            parts.append("low novelty")

        if c.category == "home":
            room = next((t for t in c.tags if t in ("master_bedroom", "spare_bedroom", "living_room")), None)
            if room:
                parts.append(f"room: {room}")

        return " | ".join(parts)
