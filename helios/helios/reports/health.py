"""Helios v7 — Health diary report generation.

Produces a structured daily health diary report from a metrics dictionary
(no live DB required).  Outputs JSON and Markdown using the atomic-write
helpers in :mod:`helios.reports`.

Semantic rules flag observations but **never** imply medical diagnosis.
Language uses "observed with" phrasing, never causation.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import REPORT_SCHEMA_VERSION, write_report_json, write_report_markdown

logger = logging.getLogger("helios.reports.health")

# ── Metric key normalisation ────────────────────────────────────────────

# Mapping from canonical metric names to the keys callers may supply.
_METRIC_ALIASES: dict[str, list[str]] = {
    "sleep_hours": ["sleep_hours", "sleep.hours", "sleep"],
    "steps": ["steps", "activity.steps_daily", "steps_daily"],
    "active_minutes": ["active_minutes", "activity.minutes_daily", "activity_minutes"],
    "resting_hr": ["resting_hr", "health.resting_hr", "resting_heart_rate", "hr"],
    "hrv_ms": ["hrv_ms", "health.hrv_ms", "hrv"],
    "workout_minutes": ["workout_minutes", "workout", "exercise_minutes"],
    "protein_g": ["protein_g", "protein", "nutrition.protein_g"],
    "mood_score": ["mood_score", "mood", "mood.score_daily", "mood_score_daily"],
}


def _resolve_metric(metrics: dict[str, Any], canonical: str) -> Any:
    """Return the first matching value for *canonical* from *metrics*."""
    aliases = _METRIC_ALIASES.get(canonical, [canonical])
    for alias in aliases:
        if alias in metrics and metrics[alias] is not None:
            return metrics[alias]
    return None


# ── Semantic flag evaluation ────────────────────────────────────────────

FORBIDDEN_DIAGNOSIS_WORDS = frozenset({
    "diagnosis", "diagnosed", "condition", "disease", "treatment",
    "prescription", "medication", "therapy", "symptom", "clinical",
})


def _evaluate_flags(metrics: dict[str, Any]) -> list[dict[str, str]]:
    """Apply semantic health rules and return a list of flag dicts.

    Each flag has ``flag``, ``metric``, and ``detail`` keys.  Language is
    intentionally observational ("observed with", "noted") — never diagnostic.
    """
    flags: list[dict[str, str]] = []

    sleep_hours = _resolve_metric(metrics, "sleep_hours")
    if sleep_hours is not None:
        if sleep_hours < 5:
            flags.append({
                "flag": "low_sleep",
                "metric": "sleep_hours",
                "detail": f"observed with {sleep_hours:.1f}h sleep (below 5h threshold)",
            })
        if sleep_hours > 10:
            flags.append({
                "flag": "long_sleep",
                "metric": "sleep_hours",
                "detail": f"observed with {sleep_hours:.1f}h sleep (above 10h threshold)",
            })

    active_minutes = _resolve_metric(metrics, "active_minutes")
    if active_minutes is not None and active_minutes > 60:
        flags.append({
            "flag": "high_activity",
            "metric": "active_minutes",
            "detail": f"observed with {active_minutes:.0f} active minutes (above 60min threshold)",
        })

    resting_hr = _resolve_metric(metrics, "resting_hr")
    if resting_hr is not None and resting_hr > 80:
        flags.append({
            "flag": "high_resting_hr",
            "metric": "resting_hr",
            "detail": f"observed with resting HR {resting_hr:.0f} bpm (above 80 bpm threshold)",
        })

    return flags


# ── Confidence determination ──────────────────────────────────────────

def _determine_confidence(metrics: dict[str, Any]) -> str:
    """Return confidence level based on data availability.

    Returns one of: ``high``, ``medium``, ``low``, ``needs_review``.
    """
    key_count = sum(
        1 for canonical in _METRIC_ALIASES
        if _resolve_metric(metrics, canonical) is not None
    )
    sleep = _resolve_metric(metrics, "sleep_hours")
    steps = _resolve_metric(metrics, "steps")

    if key_count >= 5 and sleep is not None and steps is not None:
        return "high"
    if key_count >= 3:
        return "medium"
    if key_count >= 1:
        return "low"
    return "needs_review"


# ── Narrative generation ───────────────────────────────────────────────

def _build_narrative(metrics: dict[str, Any], flags: list[dict[str, str]]) -> str:
    """Build a short factual one-sentence narrative for the diary day."""
    parts: list[str] = []

    sleep = _resolve_metric(metrics, "sleep_hours")
    if sleep is not None:
        parts.append(f"slept {sleep:.1f}h")

    steps = _resolve_metric(metrics, "steps")
    active = _resolve_metric(metrics, "active_minutes")
    if steps is not None and active is not None:
        parts.append(f"logged {int(steps)} steps / {int(active)} min active")
    elif steps is not None:
        parts.append(f"logged {int(steps)} steps")
    elif active is not None:
        parts.append(f"logged {int(active)} min active")

    hr = _resolve_metric(metrics, "resting_hr")
    if hr is not None:
        parts.append(f"resting HR {int(hr)} bpm")

    mood = _resolve_metric(metrics, "mood_score")
    if mood is not None:
        parts.append(f"mood {mood:.1f}/10")

    if not parts:
        return "No health metrics available for this day."

    base = "; ".join(parts) + "."
    if flags:
        flag_labels = ", ".join(f["flag"] for f in flags)
        base = base.rstrip(".") + f" (flags: {flag_labels})."
    return base


# ── Missing-metric detection ──────────────────────────────────────────

def _find_gaps(metrics: dict[str, Any]) -> list[str]:
    """Return a list of canonical metric names that are missing/None."""
    gaps: list[str] = []
    for canonical in _METRIC_ALIASES:
        if _resolve_metric(metrics, canonical) is None:
            gaps.append(canonical)
    return gaps


# ── HealthDiaryReport class ───────────────────────────────────────────

class HealthDiaryReport:
    """Generate a daily health diary report from a metrics dictionary.

    Parameters
    ----------
    metrics:
        Dict of health metric values.  Keys may use any alias listed in
        ``_METRIC_ALIASES`` (e.g. ``"sleep_hours"``, ``"sleep.hours"``).
    report_date:
        The date this report covers.  Defaults to today (UTC).
    """

    def __init__(
        self,
        metrics: dict[str, Any],
        report_date: date | str | None = None,
    ) -> None:
        self.metrics = dict(metrics)
        if report_date is None:
            self.report_date = datetime.now(timezone.utc).date()
        elif isinstance(report_date, str):
            self.report_date = date.fromisoformat(report_date)
        else:
            self.report_date = report_date

    # ── Build report dict ──────────────────────────────────────────────

    def build(self) -> dict[str, Any]:
        """Return the full report as a dict matching the report.v1 schema."""
        date_str = self.report_date.isoformat()
        flags = _evaluate_flags(self.metrics)
        confidence = _determine_confidence(self.metrics)
        gaps = _find_gaps(self.metrics)
        narrative = _build_narrative(self.metrics, flags)

        # Confidence adjusted if any gaps in core metrics
        core_missing = [g for g in gaps if g in ("sleep_hours", "steps")]
        if core_missing and confidence == "high":
            confidence = "medium"

        items: list[dict[str, Any]] = []

        # Sleep
        sleep = _resolve_metric(self.metrics, "sleep_hours")
        items.append({
            "key": "sleep_hours",
            "value": sleep,
            "unit": "hours",
            "confidence": "observed" if sleep is not None else "missing",
        })

        # Steps / active minutes
        steps = _resolve_metric(self.metrics, "steps")
        items.append({
            "key": "steps",
            "value": steps,
            "unit": "count",
            "confidence": "observed" if steps is not None else "missing",
        })
        active = _resolve_metric(self.metrics, "active_minutes")
        items.append({
            "key": "active_minutes",
            "value": active,
            "unit": "minutes",
            "confidence": "observed" if active is not None else "missing",
        })

        # Resting HR (only if fresh/available)
        hr = _resolve_metric(self.metrics, "resting_hr")
        if hr is not None:
            items.append({
                "key": "resting_hr",
                "value": hr,
                "unit": "bpm",
                "confidence": "observed",
            })

        # HRV
        hrv = _resolve_metric(self.metrics, "hrv_ms")
        if hrv is not None:
            items.append({
                "key": "hrv_ms",
                "value": hrv,
                "unit": "ms",
                "confidence": "observed",
            })

        # Workout
        workout = _resolve_metric(self.metrics, "workout_minutes")
        if workout is not None:
            items.append({
                "key": "workout_minutes",
                "value": workout,
                "unit": "minutes",
                "confidence": "observed",
            })

        # Protein
        protein = _resolve_metric(self.metrics, "protein_g")
        if protein is not None:
            items.append({
                "key": "protein_g",
                "value": protein,
                "unit": "grams",
                "confidence": "observed",
            })

        # Mood
        mood = _resolve_metric(self.metrics, "mood_score")
        if mood is not None:
            items.append({
                "key": "mood_score",
                "value": mood,
                "unit": "1-10",
                "confidence": "observed",
            })

        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_type": "health",
            "period_start": date_str,
            "period_end": date_str,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "confidence": confidence,
            "summary": narrative,
            "items": items,
            "flags": flags,
            "gaps": gaps,
            "privacy_level": "safe_for_user_dm",
        }

    # ── Convenience write methods ──────────────────────────────────────

    def write_json(self, data_dir: Path | str | None = None) -> Path:
        """Build the report and write it as JSON."""
        return write_report_json(self.build(), "health_diary", self.report_date.isoformat(), data_dir)

    def write_markdown(self, data_dir: Path | str | None = None) -> Path:
        """Build the report and write it as Markdown."""
        return write_report_markdown(self.build(), "health_diary", self.report_date.isoformat(), data_dir)

    def write_all(self, data_dir: Path | str | None = None) -> dict[str, Path]:
        """Write both JSON and Markdown, return ``{"json": Path, "markdown": Path}``."""
        return {
            "json": self.write_json(data_dir),
            "markdown": self.write_markdown(data_dir),
        }


# ── Markdown rendering ────────────────────────────────────────────────

def render_markdown(data: dict[str, Any]) -> str:
    """Render a health-diary report dict to a human-readable Markdown string."""
    lines: list[str] = []

    date_str = data.get("period_start", "unknown date")
    lines.append(f"# 🩺 Health Diary — {date_str}")
    lines.append("")

    # Summary
    summary = data.get("summary", "")
    if summary:
        lines.append(f"> **Summary**: {summary}")
        lines.append("")

    # Confidence
    confidence = data.get("confidence", "unknown")
    lines.append(f"- **Confidence**: {confidence}")
    lines.append("")

    # Items table
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value | Unit | Confidence |")
    lines.append("|--------|-------|------|------------|")
    for item in data.get("items", []):
        val = item.get("value")
        display_val = str(val) if val is not None else "—"
        lines.append(
            f"| {item.get('key', '')} | {display_val} | {item.get('unit', '')} | {item.get('confidence', '')} |"
        )
    lines.append("")

    # Flags
    flags = data.get("flags", [])
    if flags:
        lines.append("## Flags")
        lines.append("")
        for f in flags:
            lines.append(f"- **{f['flag']}**: {f['detail']}")
        lines.append("")

    # Gaps
    gaps = data.get("gaps", [])
    if gaps:
        lines.append("## Missing Metrics")
        lines.append("")
        for g in gaps:
            lines.append(f"- {g}")
        lines.append("")

    # Footer
    lines.append("---")
    lines.append(f"*Generated at {data.get('generated_at', 'unknown')} — privacy: {data.get('privacy_level', 'unknown')}*")
    lines.append("")

    return "\n".join(lines)


# ── Diagnosis-word check (used by tests) ────────────────────────────────

def contains_diagnosis_language(text: str) -> bool:
    """Return True if *text* contains forbidden medical-diagnosis words."""
    lower = text.lower()
    return any(word in lower for word in FORBIDDEN_DIAGNOSIS_WORDS)