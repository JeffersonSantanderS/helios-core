"""
Helios v6 — Narrative Templates (Phase 4).

Deterministic template engine for generating
operational summaries from compressed/salient timeline data.

Every narrative statement carries:
  - statement text
  - evidence (timeline_event IDs, session IDs, correlation IDs)
  - confidence (computed from source confidence + evidence count)
  - template_id (which rule generated it)
  - window (morning, work_block, evening, full_day)

Design: deterministic-first, no LLM, no psychological profiling,
         no invented causality, no autonomous loops.
"""
from __future__ import annotations

import json, logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("helios.narrative_templates")

# ==========================================================================
# DATA STRUCTURES
# ==========================================================================

@dataclass
class NarrativeStatement:
    """One grounded narrative sentence with full evidence chain."""
    statement: str
    evidence: list[str]          # event IDs, session IDs, correlation IDs
    confidence: float            # 0.0–1.0
    template_id: str             # which template produced this
    window: str = "full_day"     # morning, work_block, evening, full_day
    importance: float = 0.5
    generated_by: str = "deterministic_template"

    def to_dict(self) -> dict:
        return {
            "statement": self.statement,
            "evidence": self.evidence,
            "confidence": round(self.confidence, 3),
            "template_id": self.template_id,
            "window": self.window,
            "importance": round(self.importance, 3),
            "generated_by": self.generated_by,
        }


@dataclass
class NarrativeTemplate:
    """A template rule: trigger conditions → narrative output."""
    id: str                         # unique template ID
    requires: list[str]             # event types or conditions needed
    min_confidence: float = 0.7     # minimum confidence to emit
    importance_floor: float = 0.4   # minimum importance to consider
    window: str = "full_day"

    def matches(self, ctx: dict) -> tuple[bool, float, list[str]]:
        """Check if this template matches the current context.
        Returns (matched, confidence, evidence_ids)."""
        raise NotImplementedError

    def render(self, ctx: dict, evidence: list[str]) -> NarrativeStatement:
        """Render the narrative statement from context + evidence."""
        raise NotImplementedError


# ==========================================================================
# TEMPLATE IMPLEMENTATIONS
# ==========================================================================

class SleepAnomalyTemplate(NarrativeTemplate):
    """Sleep significantly above or below baseline."""

    def __init__(self):
        super().__init__(
            id="sleep_anomaly_v1",
            requires=["sleep_anomaly"],
            min_confidence=0.7,
            importance_floor=0.6,
        )

    def matches(self, ctx: dict) -> tuple[bool, float, list[str]]:
        anomaly = ctx.get("sleep_anomaly")
        if not anomaly:
            return False, 0, []
        value = anomaly.get("value", 0)
        baseline = anomaly.get("baseline_avg", 7.0)
        z = anomaly.get("z_score", 0)
        eid = anomaly.get("timeline_event_id", "")
        evidence = [eid] if eid else []

        if z < 2.0:
            return False, 0, []
        conf = min(0.95, 0.6 + z / 10)
        direction = anomaly.get("direction", "")
        return True, conf, evidence

    def render(self, ctx: dict, evidence: list[str]) -> NarrativeStatement:
        a = ctx["sleep_anomaly"]
        value = a.get("value", 0)
        baseline = a.get("baseline_avg", 7.0)
        z = a.get("z_score", 0)

        # Use semantic check against healthy baseline (7.0h), not
        # the anomaly detector's computed baseline which may be
        # corrupted by sparse/corrupted data.
        if value < 5.0:
            # Unambiguously low sleep regardless of what anomaly detector says
            if z >= 4.0 or value < 3.0:
                stmt = f"Sleep was critically low at {value:.1f}h — well below healthy minimum (z={z:.1f})"
            elif value < 4.0:
                stmt = f"Sleep dropped significantly to {value:.1f}h, below the 5h threshold"
            else:
                stmt = f"Sleep came in low at {value:.1f}h (healthy minimum: 5h)"
        elif value > 10.0:
            stmt = f"Sleep was unusually long at {value:.1f}h, above typical range"
        elif value > 9.0:
            stmt = f"Sleep ran long at {value:.1f}h"
        elif value >= 7.0:
            stmt = f"Sleep was healthy at {value:.1f}h"
        else:
            # 5.0–7.0h: below ideal but not alarming
            if z >= 3.0:
                stmt = f"Sleep ran short at {value:.1f}h — below your 7h target"
            else:
                stmt = f"Sleep measured {value:.1f}h, slightly below target"

        return NarrativeStatement(
            statement=stmt, evidence=evidence,
            confidence=min(0.95, 0.6 + z / 10),
            template_id=self.id, importance=a.get("importance", 0.7),
        )


class ActivityAnomalyTemplate(NarrativeTemplate):
    """Activity significantly above or below baseline."""

    def __init__(self):
        super().__init__(
            id="activity_anomaly_v1",
            requires=["activity_anomaly"],
            min_confidence=0.7,
            importance_floor=0.6,
        )

    def matches(self, ctx: dict) -> tuple[bool, float, list[str]]:
        a = ctx.get("activity_anomaly")
        if not a:
            return False, 0, []
        z = a.get("z_score", 0)
        if z < 2.0:
            return False, 0, []
        eid = a.get("timeline_event_id", "")
        return True, min(0.95, 0.6 + z / 10), [eid] if eid else []

    def render(self, ctx: dict, evidence: list[str]) -> NarrativeStatement:
        a = ctx["activity_anomaly"]
        value = a.get("value", 0)
        baseline = a.get("baseline_avg", 30)
        direction = a.get("direction", "low")
        z = a.get("z_score", 0)

        if direction == "high":
            stmt = f"Activity was unusually high at {value:.0f} min — far above your typical {baseline:.0f} min"
        else:
            stmt = f"Activity dropped to {value:.0f} min, below your {baseline:.0f} min baseline"

        return NarrativeStatement(
            statement=stmt, evidence=evidence,
            confidence=min(0.95, 0.6 + z / 10),
            template_id=self.id, importance=a.get("importance", 0.7),
        )


class HeartRateTemplate(NarrativeTemplate):
    """Resting HR anomaly."""

    def __init__(self):
        super().__init__(
            id="resting_hr_v1",
            requires=["hr_anomaly"],
            min_confidence=0.7,
            importance_floor=0.6,
        )

    def matches(self, ctx: dict) -> tuple[bool, float, list[str]]:
        a = ctx.get("hr_anomaly")
        if not a:
            return False, 0, []
        z = a.get("z_score", 0)
        if z < 2.0:
            return False, 0, []
        eid = a.get("timeline_event_id", "")
        return True, min(0.95, 0.6 + z / 10), [eid] if eid else []

    def render(self, ctx: dict, evidence: list[str]) -> NarrativeStatement:
        a = ctx["hr_anomaly"]
        value = a.get("value", 0)
        baseline = a.get("baseline_avg", 70)
        direction = a.get("direction", "normal")

        if direction == "low":
            stmt = f"Resting heart rate was unusually low at {value:.0f} bpm (baseline: {baseline:.0f})"
        elif direction == "high":
            stmt = f"Resting heart rate was elevated at {value:.0f} bpm (baseline: {baseline:.0f})"
        else:
            stmt = f"Resting heart rate measured {value:.0f} bpm (baseline: {baseline:.0f})"

        return NarrativeStatement(
            statement=stmt, evidence=evidence,
            confidence=min(0.95, 0.6 + a.get("z_score", 0) / 10),
            template_id=self.id, importance=a.get("importance", 0.7),
        )


class MoodShiftTemplate(NarrativeTemplate):
    """Significant mood change day-over-day."""

    def __init__(self):
        super().__init__(
            id="mood_shift_v1",
            requires=["mood_data"],
            min_confidence=0.7,
            importance_floor=0.5,
        )

    def matches(self, ctx: dict) -> tuple[bool, float, list[str]]:
        mood = ctx.get("mood_data", {})
        prev = mood.get("previous_score")
        curr = mood.get("score")
        if prev is None or curr is None:
            return False, 0, []
        delta = abs(curr - prev)
        eid = mood.get("timeline_event_id", "")
        if delta >= 4:
            return True, 0.9, [eid] if eid else []
        if delta >= 2:
            return True, 0.75, [eid] if eid else []
        return False, 0, []

    def render(self, ctx: dict, evidence: list[str]) -> NarrativeStatement:
        mood = ctx["mood_data"]
        prev, curr = mood.get("previous_score", 5), mood.get("score", 5)
        label = mood.get("label", "")
        delta = curr - prev
        conf = 0.9 if abs(delta) >= 4 else 0.75

        if delta >= 4:
            stmt = f"Mood improved sharply: {label} ({curr}/9), up from {prev}/9 yesterday"
        elif delta <= -4:
            stmt = f"Mood dropped notably: {label} ({curr}/9), down from {prev}/9 yesterday"
        elif delta >= 2:
            stmt = f"Mood improved to {label} ({curr}/9), up from {prev}/9"
        else:
            stmt = f"Mood shifted to {label} ({curr}/9), down from {prev}/9"

        return NarrativeStatement(
            statement=stmt, evidence=evidence, confidence=conf,
            template_id=self.id, importance=mood.get("importance", 0.6),
        )


class CorrelationImpactTemplate(NarrativeTemplate):
    """Significant correlation linked to today's data."""

    def __init__(self):
        super().__init__(
            id="correlation_impact_v1",
            requires=["strong_correlation", "sleep_anomaly"],
            min_confidence=0.7,
            importance_floor=0.65,
        )

    def matches(self, ctx: dict) -> tuple[bool, float, list[str]]:
        corr = ctx.get("strong_correlation")
        if not corr or corr.get("strength") not in ("strong", "moderate"):
            return False, 0, []
        # Need an anomaly linked to this correlation
        sleep_a = ctx.get("sleep_anomaly")
        activity_a = ctx.get("activity_anomaly")
        if not sleep_a and not activity_a:
            return False, 0, []
        evidence = [corr.get("correlation_id", "")]
        if sleep_a:
            evidence.append(sleep_a.get("timeline_event_id", ""))
        if activity_a:
            evidence.append(activity_a.get("timeline_event_id", ""))
        evidence = [e for e in evidence if e]
        r = abs(corr.get("r", 0))
        return True, min(0.9, 0.65 + r), evidence

    def render(self, ctx: dict, evidence: list[str]) -> NarrativeStatement:
        corr = ctx["strong_correlation"]
        ma = corr.get("metric_a", "?"); mb = corr.get("metric_b", "?")
        r = abs(corr.get("r", 0))
        labels = {"sleep.hours": "sleep", "activity.minutes_daily": "activity",
                  "mood.score_daily": "mood", "resting_heart_rate.avg_daily": "resting HR"}
        la, lb = labels.get(ma, ma), labels.get(mb, mb)

        if r >= 0.8:
            stmt = f"Strong link between {la} and {lb} (r={r:.2f}) — today's data reflects this pattern"
        else:
            stmt = f"Moderate relationship between {la} and {lb} (r={r:.2f}) observed"

        return NarrativeStatement(
            statement=stmt, evidence=evidence,
            confidence=min(0.9, 0.65 + r),
            template_id=self.id, importance=corr.get("importance", 0.7),
        )


class FocusContinuityTemplate(NarrativeTemplate):
    """Work or gaming focus block quality assessment."""

    def __init__(self):
        super().__init__(
            id="focus_continuity_v1",
            requires=["focus_session"],
            min_confidence=0.65,
            importance_floor=0.4,
            window="work_block",
        )

    def matches(self, ctx: dict) -> tuple[bool, float, list[str]]:
        sess = ctx.get("focus_session")
        if not sess:
            return False, 0, []
        dur_h = sess.get("duration_secs", 0) / 3600
        state = sess.get("state", "")
        if dur_h < 0.5 or state not in ("working", "gaming"):
            return False, 0, []
        sid = sess.get("session_id", "")
        evidence = [sid] if sid else []
        conf = 0.65 + min(0.3, dur_h / 10)
        return True, conf, evidence

    def render(self, ctx: dict, evidence: list[str]) -> NarrativeStatement:
        sess = ctx["focus_session"]
        dur_h = sess.get("duration_secs", 0) / 3600
        state = sess.get("state", "working")
        label = "Work" if state == "working" else "Gaming"
        hours_fmt = f"{dur_h:.1f}h" if dur_h >= 1 else f"{dur_h*60:.0f}m"

        if dur_h >= 4:
            stmt = f"Deep {label.lower()} block: {hours_fmt} continuous — strong focus signal"
        elif dur_h >= 2:
            stmt = f"Solid {label.lower()} session: {hours_fmt} sustained"
        else:
            stmt = f"{label} session: {hours_fmt}"

        return NarrativeStatement(
            statement=stmt, evidence=evidence,
            confidence=min(0.9, 0.65 + dur_h / 10),
            template_id=self.id, importance=sess.get("importance", 0.5),
        )


class LowConfidenceFallback(NarrativeTemplate):
    """When data is too sparse for confident statements, provide a measured summary."""

    def __init__(self):
        super().__init__(
            id="low_confidence_fallback_v1",
            requires=[],
            min_confidence=0.0,
            importance_floor=0.0,
        )

    def matches(self, ctx: dict) -> tuple[bool, float, list[str]]:
        events_today = ctx.get("events_today", 0)
        return True, min(0.6, events_today / 50), []

    def render(self, ctx: dict, evidence: list[str]) -> NarrativeStatement:
        events_today = ctx.get("events_today", 0)
        sessions_today = ctx.get("sessions_today", 0)
        date = ctx.get("date_key", "today")

        if events_today < 10:
            stmt = f"Light data day — {events_today} events recorded. Narrative confidence is limited."
        elif sessions_today == 0:
            stmt = f"Data collected ({events_today} events) but no significant sessions detected."
        else:
            stmt = f"{sessions_today} operational session{'s' if sessions_today != 1 else ''} tracked. Routine day."

        return NarrativeStatement(
            statement=stmt, evidence=evidence, confidence=0.5,
            template_id=self.id, importance=0.3,
        )


# ==========================================================================
# TEMPLATE REGISTRY
# ==========================================================================

ALL_TEMPLATES: list[NarrativeTemplate] = [
    SleepAnomalyTemplate(),
    ActivityAnomalyTemplate(),
    HeartRateTemplate(),
    MoodShiftTemplate(),
    CorrelationImpactTemplate(),
    FocusContinuityTemplate(),
    LowConfidenceFallback(),
]

TEMPLATES_BY_ID: dict[str, NarrativeTemplate] = {t.id: t for t in ALL_TEMPLATES}


# ==========================================================================
# MATCHING ENGINE
# ==========================================================================

def build_context(db_conn, date_key: str) -> dict:
    """Gather all relevant data for narrative template matching."""
    ctx: dict[str, Any] = {"date_key": date_key}

    # Today's timeline event count
    r = db_conn.execute(
        "SELECT COUNT(*) FROM timeline_events WHERE date_key=?", (date_key,)
    ).fetchone()
    ctx["events_today"] = r[0] if r else 0

    # Session count
    r = db_conn.execute(
        "SELECT COUNT(*) FROM timeline_sessions WHERE date_key=?",
        (date_key,),
    ).fetchone()
    ctx["sessions_today"] = r[0] if r else 0

    # --- Anomalies: pick strongest per metric type ---
    for metric, ctx_key in [
        ("sleep.hours", "sleep_anomaly"),
        ("activity.minutes_daily", "activity_anomaly"),
        ("resting_heart_rate.avg_daily", "hr_anomaly"),
    ]:
        rows = db_conn.execute(
            "SELECT id, ts, importance, metadata FROM timeline_events "
            "WHERE event_type='metric_anomaly' AND date_key=? ORDER BY importance DESC",
            (date_key,),
        ).fetchall()
        for row in rows:
            meta = json.loads(row[3]) if row[3] else {}
            if meta.get("metric") == metric:
                ctx[ctx_key] = {
                    "metric": metric, "value": meta.get("value"),
                    "baseline_avg": meta.get("baseline_avg"),
                    "z_score": meta.get("z_score", 0),
                    "direction": meta.get("direction", ""),
                    "importance": row[2] or 0.7,
                    "timeline_event_id": f"evt_{row[0]}",
                }
                break

    # --- Mood ---
    mood_rows = db_conn.execute(
        "SELECT id, importance, metadata FROM timeline_events "
        "WHERE event_type='mood_recorded' ORDER BY date_key DESC LIMIT 2"
    ).fetchall()
    if len(mood_rows) >= 1:
        meta = json.loads(mood_rows[0][2]) if mood_rows[0][2] else {}
        prev_score = None
        if len(mood_rows) >= 2:
            pmeta = json.loads(mood_rows[1][2]) if mood_rows[1][2] else {}
            prev_score = pmeta.get("score")
        ctx["mood_data"] = {
            "score": meta.get("score"),
            "label": meta.get("label", ""),
            "previous_score": prev_score,
            "importance": mood_rows[0][1] or 0.5,
            "timeline_event_id": f"evt_{mood_rows[0][0]}",
        }

    # --- Strongest correlation ---
    corr = db_conn.execute(
        "SELECT id, metric_a, metric_b, pearson_r, p_value, strength FROM correlations "
        "ORDER BY ABS(pearson_r) DESC LIMIT 1"
    ).fetchone()
    if corr and corr[4] in ("strong", "moderate"):
        ctx["strong_correlation"] = {
            "correlation_id": f"corr_{corr[0]}",
            "metric_a": corr[1], "metric_b": corr[2],
            "r": corr[3], "strength": corr[5],
            "importance": 0.8 if corr[5] == "strong" else 0.6,
        }

    # --- Top focus session ---
    sess = db_conn.execute(
        "SELECT id, dominant_state, duration_secs, importance, novelty "
        "FROM timeline_sessions WHERE date_key=? AND session_type='focus_block' "
        "ORDER BY duration_secs DESC LIMIT 1",
        (date_key,),
    ).fetchone()
    if sess:
        ctx["focus_session"] = {
            "session_id": f"sess_{sess[0]}",
            "state": sess[1],
            "duration_secs": sess[2],
            "importance": sess[3] or 0.5,
            "novelty": sess[4] or 0.3,
        }

    return ctx


def match_templates(ctx: dict) -> list[tuple[NarrativeTemplate, float, list[str]]]:
    """Run all templates against context, return matched (template, confidence, evidence)."""
    matches = []
    for template in ALL_TEMPLATES:
        try:
            ok, conf, evidence = template.matches(ctx)
            if ok and conf >= template.min_confidence:
                matches.append((template, conf, evidence))
        except Exception as exc:
            log.debug("Template %s match error: %s", template.id, exc)
    return matches


def render_statements(
    ctx: dict, matches: list[tuple[NarrativeTemplate, float, list[str]]]
) -> list[NarrativeStatement]:
    """Render matched templates into narrative statements."""
    statements = []
    for template, conf, evidence in matches:
        try:
            stmt = template.render(ctx, evidence)
            statements.append(stmt)
        except Exception as exc:
            log.warning("Template %s render error: %s", template.id, exc)
    return statements


def generate_narrative(db_conn, date_key: str) -> list[NarrativeStatement]:
    """Full pipeline: context → match → render."""
    ctx = build_context(db_conn, date_key)
    matches = match_templates(ctx)
    statements = render_statements(ctx, matches)

    # Sort by importance descending
    statements.sort(key=lambda s: s.importance, reverse=True)

    # Remove low-confidence fallback if we have real statements
    if len(statements) > 1:
        statements = [s for s in statements
                      if s.template_id != "low_confidence_fallback_v1"]

    log.info("Narrative: %d statements from %d template matches",
             len(statements), len(matches))
    return statements


def format_narrative_markdown(statements: list[NarrativeStatement],
                              date_key: str) -> str:
    """Format narrative statements as Discord-friendly markdown."""
    if not statements:
        return f"**📖 Helios — {date_key}**\n\nNo significant events today."

    lines = [f"**📖 Helios — {date_key}**", ""]
    current_window = None

    for s in statements:
        if s.window != current_window:
            window_labels = {
                "morning": "☀️ Morning", "work_block": "💼 Work Block",
                "evening": "🌙 Evening", "full_day": "📊 Daily Summary",
            }
            label = window_labels.get(s.window, s.window)
            if current_window is not None:
                lines.append("")
            lines.append(f"**{label}**")
            current_window = s.window
        lines.append(f"• {s.statement} _{s.confidence*100:.0f}% conf._")

    # Evidence summary
    all_evidence = []
    for s in statements:
        all_evidence.extend(s.evidence)
    unique_e = len(set(e for e in all_evidence if e))
    lines.append("")
    lines.append(f"_{len(statements)} statements · {unique_e} evidence items · deterministic_")

    return "\n".join(lines)


def format_narrative_json(statements: list[NarrativeStatement]) -> list[dict]:
    """Export narrative as JSON for programmatic consumption."""
    return [s.to_dict() for s in statements]
