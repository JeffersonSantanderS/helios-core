"""Helios v7 — Agenda report generation.

Uses the agenda helper to produce report.v1 agenda summaries
for dashboard cards and query answers.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import REPORT_SCHEMA_VERSION, write_report_json, write_report_markdown

logger = logging.getLogger("helios.reports.agenda")

DEFAULT_DATA_DIR = Path.home() / ".hermes" / "helios" / "data"


def build_agenda_report(
    focus_list: dict[str, Any],
    conflicts: list[tuple] | None = None,
    free_blocks: list[tuple] | None = None,
    report_date: Optional[str] = None,
) -> dict[str, Any]:
    """Build a report.v1 agenda summary.

    Args:
        focus_list: Dict from agenda.today_focus_list().
        conflicts: List of conflict pairs from agenda.find_conflicts().
        free_blocks: List of (start, end, duration_min) from agenda.find_free_blocks().
        report_date: ISO date string. Defaults to today.

    Returns:
        Report dict conforming to report.v1 schema.
    """
    if report_date is None:
        report_date = date.today().isoformat()

    events_count = focus_list.get("events_count", 0)
    overdue = focus_list.get("overdue_count", 0)
    next_event = focus_list.get("next_event")

    # Build confidence from data quality
    confidence = "high" if events_count > 0 else "low"
    if overdue > 0:
        confidence = "medium"

    summary_parts = []
    if events_count > 0:
        summary_parts.append(f"{events_count} event{'s' if events_count != 1 else ''}")
    else:
        summary_parts.append("No events today")

    if next_event:
        summary_parts.append(f"next at {next_event}")

    free_min = focus_list.get("free_minutes", 0)
    if free_min > 0:
        summary_parts.append(f"best free block: {free_min}min")

    if overdue > 0:
        summary_parts.append(f"{overdue} overdue")

    summary = ". ".join(summary_parts) + "."

    items = []
    if next_event:
        items.append({
            "kind": "next_event",
            "title": str(next_event) if next_event else "Unknown",
            "free_minutes": free_min,
        })
    for conflict in (conflicts or []):
        items.append({
            "kind": "conflict",
            "title": f"Overlapping: {conflict}",
        })
    for block in (free_blocks or []):
        start_str, end_str, dur = block[:3]
        items.append({
            "kind": "free_block",
            "start": str(start_str),
            "end": str(end_str),
            "duration_min": dur,
        })

    gaps = []
    if events_count == 0:
        gaps.append("No calendar data for today")
    if overdue > 0:
        gaps.append(f"{overdue} overdue items need attention")

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "agenda",
        "period_start": report_date,
        "period_end": report_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "confidence": confidence,
        "summary": summary,
        "items": items,
        "gaps": gaps,
        "privacy_level": "safe_for_user_dm",
        "events_count": events_count,
        "overdue_count": overdue,
        "free_minutes": free_min,
    }


def render_agenda_markdown(report: dict[str, Any]) -> str:
    """Render an agenda report as Markdown."""
    lines = [
        f"# Agenda — {report.get('period_start', '')}",
        "",
        f"**Summary:** {report.get('summary', 'No data')}",
        f"**Confidence:** {report.get('confidence', 'unknown')}",
        "",
    ]

    ec = report.get("events_count", 0)
    oc = report.get("overdue_count", 0)
    fm = report.get("free_minutes", 0)
    lines.append(f"- Events: {ec}")
    lines.append(f"- Overdue: {oc}")
    lines.append(f"- Free minutes: {fm}")
    lines.append("")

    for item in report.get("items", []):
        kind = item.get("kind", "unknown")
        if kind == "next_event":
            lines.append(f"⏰ **Next:** {item.get('title', 'Unknown')}")
        elif kind == "conflict":
            lines.append(f"⚠️ **Conflict:** {item.get('title', '')}")
        elif kind == "free_block":
            lines.append(
                f"✅ **Free block:** {item.get('start', '')}–{item.get('end', '')} "
                f"({item.get('duration_min', 0)}min)"
            )

    gaps = report.get("gaps", [])
    if gaps:
        lines.append("")
        lines.append("### Gaps")
        for g in gaps:
            lines.append(f"- {g}")

    return "\n".join(lines) + "\n"


def write_agenda_report(
    focus_list: dict[str, Any],
    conflicts: list[tuple] | None = None,
    free_blocks: list[tuple] | None = None,
    data_dir: Path | str | None = None,
    report_date: Optional[str] = None,
) -> Path:
    """Build and write agenda report (JSON + Markdown)."""
    report = build_agenda_report(focus_list, conflicts, free_blocks, report_date)
    date_str = report_date or date.today().isoformat()
    json_path = write_report_json(report, "agenda", date_str, data_dir)
    write_report_markdown(report, "agenda", date_str, data_dir)
    return json_path