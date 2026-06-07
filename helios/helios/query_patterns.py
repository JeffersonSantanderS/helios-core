"""Helios v7 — Personal query answer contract.

Deterministic query pattern handlers that produce structured answers
from available data. No hallucinated answers when data is missing;
gaps are explicit. Raw coordinates excluded unless debug flag.
Confidence is honest about data quality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

# ── Answer contract ───────────────────────────────────────────────────────


@dataclass
class QueryAnswer:
    """Structured answer to a personal query.

    Attributes:
        answer: Short human-readable text answer.
        confidence: One of "high", "medium", "low", "needs_review".
        evidence: List of source references (file/table/source + timestamp).
        gaps: List of missing data descriptions.
        privacy_level: Always "safe_for_user_dm" for dashboard output.
    """
    answer: str
    confidence: str
    evidence: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    privacy_level: str = "safe_for_user_dm"


# ── Helpers ───────────────────────────────────────────────────────────────

def _safe_int(val: Any, default: int = 0) -> int:
    """Safely convert a value to int, returning default on failure."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Safely convert a value to float, returning default on failure."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _fmt_hours(hours: float | None) -> str:
    """Format hours value for display, handling None."""
    if hours is None:
        return "unknown"
    if hours == int(hours):
        return f"{int(hours)}h"
    return f"{hours:.1f}h"


# ── Query handlers ─────────────────────────────────────────────────────────


def handle_where_was_i(
    location_data: dict[str, Any] | None = None,
    **kwargs: Any,
) -> QueryAnswer:
    """Answer: Where was I today / recently?"""
    if not location_data:
        return QueryAnswer(
            answer="Location data unavailable.",
            confidence="needs_review",
            gaps=["No location data provided."],
        )

    # Never include raw coordinates unless debug flag
    raw_zone = location_data.get("zone_label", "")
    city = location_data.get("city", "")
    is_home = location_data.get("is_home", False)
    freshness = location_data.get("freshness_secs")
    source = location_data.get("source", "unknown")
    last_updated = location_data.get("last_updated")

    # Build zone_label: prefer explicit zone, then is_home, then city
    if raw_zone and raw_zone != "away":
        zone_label = raw_zone
    elif is_home:
        zone_label = "home"
    elif city:
        zone_label = city
    else:
        zone_label = ""

    if not zone_label:
        return QueryAnswer(
            answer="Location data exists but no zone or city available.",
            confidence="low",
            evidence=[f"location source={source}"],
            gaps=["Missing zone_label and city."],
        )

    # Build answer text using zone_label (never raw coordinates)
    if zone_label == "home" or (raw_zone and raw_zone != "away"):
        parts = [f"Near {zone_label}"]
    elif city:
        parts = [f"In {city}"]
    else:
        parts = [f"Via {source}"]
    if freshness is not None:
        if freshness < 300:
            parts.append("(very fresh)")
        elif freshness < 3600:
            parts.append(f"(updated {int(freshness / 60)}m ago)")
        else:
            parts.append(f"(updated {int(freshness / 3600)}h ago)")

    answer = " ".join(parts) + "."

    # Determine confidence
    confidence = "low"
    if freshness is not None:
        if freshness < 300:
            confidence = "high"
        elif freshness < 3600:
            confidence = "medium"

    evidence = []
    if source:
        evidence.append(f"location source={source}")
    if last_updated:
        evidence.append(f"location last_updated={last_updated}")

    return QueryAnswer(
        answer=answer,
        confidence=confidence,
        evidence=evidence,
    )


def handle_when_leave_work(
    work_hours_data: dict[str, Any] | None = None,
    location_data: dict[str, Any] | None = None,
    **kwargs: Any,
) -> QueryAnswer:
    """Answer: When did I leave work today?"""
    if not work_hours_data and not location_data:
        return QueryAnswer(
            answer="No work hours or location data available.",
            confidence="needs_review",
            gaps=["No work_hours_data or location_data provided."],
        )

    # Try work hours data first
    if work_hours_data:
        days = work_hours_data.get("days", [])
        # Find today's entry
        from datetime import date
        today_str = date.today().isoformat()
        today_entry = None
        for d in days:
            if d.get("date") == today_str:
                today_entry = d
                break

        if today_entry and today_entry.get("kind") == "work":
            end_time = today_entry.get("end", "unknown")
            confidence_raw = today_entry.get("confidence", "low")
            return QueryAnswer(
                answer=f"Left work at {end_time} today (recorded).",
                confidence=confidence_raw,
                evidence=[
                    f"work_hours date={today_entry.get('date')}",
                    f"work_hours source={today_entry.get('source', 'unknown')}",
                ],
            )

        if today_entry and today_entry.get("kind") == "needs_review":
            return QueryAnswer(
                answer=f"Work hours need review for today: {today_entry.get('note', 'unknown reason')}.",
                confidence="needs_review",
                evidence=[f"work_hours date={today_str}"],
                gaps=[today_entry.get("note", "Work hours not yet reviewed.")],
            )

    # Fall back to location data heuristic
    if location_data and location_data.get("zone_label") == "home":
        return QueryAnswer(
            answer="Currently at home — likely left work already.",
            confidence="medium",
            evidence=["location zone=home"],
        )

    return QueryAnswer(
        answer="Could not determine when you left work.",
        confidence="low",
        gaps=["No work hours data for today.", "Location does not indicate home."],
    )


def handle_hours_worked(
    work_hours_data: dict[str, Any] | None = None,
    **kwargs: Any,
) -> QueryAnswer:
    """Answer: How many hours did I work?"""
    if not work_hours_data:
        return QueryAnswer(
            answer="No work hours data available.",
            confidence="needs_review",
            gaps=["No work_hours_data provided."],
        )

    days = work_hours_data.get("days", [])
    if not days:
        return QueryAnswer(
            answer="No work hours recorded yet for this period.",
            confidence="needs_review",
            gaps=["work_hours_data.days is empty."],
        )

    total_hours = sum(_safe_float(d.get("paid_hours", 0)) for d in days)
    work_days = [d for d in days if d.get("kind") == "work"]
    review_days = [d for d in days if d.get("kind") == "needs_review"]

    period_label = work_hours_data.get("current_pay_period", "this period")
    confidence_summary = work_hours_data.get("confidence_counts", {})

    # Determine confidence
    confidence = "medium"
    if review_days:
        confidence = "needs_review"
    elif confidence_summary.get("high", 0) > 0 and not review_days:
        confidence = "high"

    answer = f"{_fmt_hours(total_hours)} worked across {len(work_days)} day(s) in {period_label}"
    if review_days:
        answer += f"; {len(review_days)} day(s) need review"

    evidence = [
        f"work_hours period={period_label}",
        f"work_hours total_paid_hours={total_hours}",
    ]

    gaps = []
    if review_days:
        for rd in review_days:
            gaps.append(f"Day {rd.get('date', '?')} needs review: {rd.get('note', 'unknown')}")

    return QueryAnswer(
        answer=answer,
        confidence=confidence,
        evidence=evidence,
        gaps=gaps,
    )


def handle_sleep(
    health_data: dict[str, Any] | None = None,
    **kwargs: Any,
) -> QueryAnswer:
    """Answer: How did I sleep?"""
    if not health_data:
        return QueryAnswer(
            answer="No health data available.",
            confidence="needs_review",
            gaps=["No health_data provided."],
        )

    sleep_hours = health_data.get("sleep_hours")
    confidence = health_data.get("confidence", "needs_review")

    if sleep_hours is None:
        return QueryAnswer(
            answer="Sleep data not available for today.",
            confidence="needs_review",
            gaps=["sleep_hours missing from health_data."],
        )

    # Determine quality description
    quality = "a normal amount"
    if sleep_hours < 5:
        quality = "very little sleep"
    elif sleep_hours < 7:
        quality = "less than recommended"
    elif sleep_hours >= 9:
        quality = "plenty of sleep"

    answer = f"Slept {_fmt_hours(sleep_hours)} — {quality}."

    evidence = [f"health_diary sleep_hours={sleep_hours}"]

    # Add extra context if available
    active_minutes = health_data.get("active_minutes")
    if active_minutes is not None:
        evidence.append(f"health_diary active_minutes={active_minutes}")

    gaps = []
    stale_warnings = health_data.get("stale_data_warnings", [])
    if stale_warnings:
        gaps.extend(stale_warnings)

    return QueryAnswer(
        answer=answer,
        confidence=confidence,
        evidence=evidence,
        gaps=gaps,
    )


def handle_weekly_change(
    work_hours_data: dict[str, Any] | None = None,
    health_data: dict[str, Any] | None = None,
    location_data: dict[str, Any] | None = None,
    spotify_data: dict[str, Any] | None = None,
    **kwargs: Any,
) -> QueryAnswer:
    """Answer: What changed this week?"""
    changes: list[str] = []
    evidence: list[str] = []
    gaps: list[str] = []
    parts: list[str] = []

    if work_hours_data:
        days = work_hours_data.get("days", [])
        work_days = [d for d in days if d.get("kind") == "work"]
        total = sum(_safe_float(d.get("paid_hours", 0)) for d in work_days)
        parts.append(f"worked {_fmt_hours(total)}")
        evidence.append(f"work_hours days={len(work_days)}")
    else:
        gaps.append("No work hours data for this week.")

    if health_data:
        sleep = health_data.get("sleep_hours")
        if sleep is not None:
            parts.append(f"slept {_fmt_hours(sleep)}")
            evidence.append(f"health_diary sleep_hours={sleep}")
        else:
            gaps.append("No sleep data available.")

        active = health_data.get("active_minutes")
        if active is not None:
            parts.append(f"active for {int(active)} minutes")
            evidence.append(f"health_diary active_minutes={active}")
    else:
        gaps.append("No health data available.")

    if location_data:
        zone = location_data.get("zone_label", "")
        if zone and zone != "unknown":
            parts.append(f"near {zone}")
            evidence.append(f"location zone={zone}")
    else:
        gaps.append("No location data available.")

    if spotify_data:
        total_min = spotify_data.get("total_minutes")
        if total_min is not None:
            parts.append(f"listened to {int(total_min)} min of music")
            evidence.append(f"spotify total_minutes={total_min}")
    else:
        gaps.append("No Spotify data available.")

    if not parts:
        answer = "No data available to summarize changes this week."
        confidence = "needs_review"
    else:
        answer = "This week: " + ", ".join(parts) + "."
        confidence = "medium" if len(evidence) >= 2 else "low"

    return QueryAnswer(
        answer=answer,
        confidence=confidence,
        evidence=evidence,
        gaps=gaps,
    )


def handle_needs_review(
    work_hours_data: dict[str, Any] | None = None,
    **kwargs: Any,
) -> QueryAnswer:
    """Answer: What needs review?"""
    if not work_hours_data:
        return QueryAnswer(
            answer="No work hours data available to check.",
            confidence="needs_review",
            gaps=["No work_hours_data provided."],
        )

    needs = work_hours_data.get("needs_review", [])
    if not needs:
        # Also check days list
        days = work_hours_data.get("days", [])
        needs = [
            {"date": d.get("date"), "reason": d.get("note", "")}
            for d in days
            if d.get("kind") == "needs_review"
        ]

    if not needs:
        return QueryAnswer(
            answer="Nothing needs review — all work hours are confirmed.",
            confidence="high",
            evidence=["work_hours no needs_review entries"],
        )

    reasons = []
    for n in needs:
        date = n.get("date", "unknown date")
        reason = n.get("reason", "unknown reason")
        reasons.append(f"{date}: {reason}")

    answer = f"{len(needs)} item(s) need review: " + "; ".join(reasons)
    return QueryAnswer(
        answer=answer,
        confidence="medium",
        evidence=[f"work_hours needs_review count={len(needs)}"],
        gaps=[f"Review needed for: {', '.join(n.get('date', '?') for n in needs)}"],
    )


def handle_why_alerted(
    module_staleness_data: dict[str, Any] | None = None,
    location_data: dict[str, Any] | None = None,
    health_data: dict[str, Any] | None = None,
    **kwargs: Any,
) -> QueryAnswer:
    """Answer: Why was I alerted?"""
    reasons: list[str] = []
    evidence: list[str] = []
    gaps: list[str] = []

    if module_staleness_data:
        stale = module_staleness_data.get("stale_modules", [])
        modules = module_staleness_data.get("modules", [])
        if stale:
            reasons.append(f"{len(stale)} module(s) stale: {', '.join(stale)}")
            evidence.append(f"module_staleness stale_modules={stale}")
        failed = [m for m in modules if m.get("state") == "failed"]
        if failed:
            names = [m["module_name"] for m in failed]
            reasons.append(f"{len(failed)} module(s) failed: {', '.join(names)}")
            evidence.append(f"module_staleness failed_modules={names}")
    else:
        gaps.append("No module staleness data provided.")

    if location_data:
        freshness = location_data.get("freshness_secs")
        if freshness is not None and freshness > 3600:
            reasons.append(f"Location data is {int(freshness / 3600)}h old")
            evidence.append(f"location freshness_secs={freshness}")
    else:
        gaps.append("No location data provided.")

    if health_data:
        stale_warnings = health_data.get("stale_data_warnings", [])
        if stale_warnings:
            reasons.append(f"Health data gaps: {', '.join(str(w) for w in stale_warnings)}")
            evidence.append("health_diary stale_data_warnings present")
    else:
        gaps.append("No health data provided.")

    if not reasons:
        return QueryAnswer(
            answer="No alert triggers found — all systems nominal.",
            confidence="high",
            evidence=evidence,
            gaps=gaps,
        )

    answer = "Alert triggered: " + "; ".join(reasons) + "."
    confidence = "medium" if len(evidence) >= 2 else "low"

    return QueryAnswer(
        answer=answer,
        confidence=confidence,
        evidence=evidence,
        gaps=gaps,
    )


# ── Pattern registry ───────────────────────────────────────────────────────

QUERY_PATTERNS: dict[str, Callable[..., QueryAnswer]] = {
    "where_was_i_today": handle_where_was_i,
    "when_did_i_leave_work": handle_when_leave_work,
    "how_many_hours_worked": handle_hours_worked,
    "how_did_i_sleep": handle_sleep,
    "what_changed_this_week": handle_weekly_change,
    "what_needs_review": handle_needs_review,
    "why_alerted": handle_why_alerted,
}


def answer_query(
    pattern: str,
    **data: Any,
) -> QueryAnswer:
    """Look up a query pattern by name and dispatch to its handler.

    Args:
        pattern: One of the keys in QUERY_PATTERNS.
        **data: Data dicts passed to the handler (work_hours_data,
                health_data, location_data, spotify_data, etc.)

    Returns:
        A QueryAnswer with answer, confidence, evidence, and gaps.

    Raises:
        KeyError: If pattern is not in QUERY_PATTERNS.
    """
    handler = QUERY_PATTERNS[pattern]
    return handler(**data)