"""Helios v7 — Location daily summary report generation.

Uses LocationZoneResolver to build report.v1 location summaries.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import REPORT_SCHEMA_VERSION, write_report_json, write_report_markdown

logger = logging.getLogger("helios.reports.location")

DEFAULT_DATA_DIR = Path.home() / ".hermes" / "helios" / "data"


def build_location_report(
    summary: dict[str, Any],
    report_date: Optional[str] = None,
) -> dict[str, Any]:
    """Build a report.v1 location summary report.

    Args:
        summary: Dict from LocationZoneResolver.daily_summary().
        report_date: ISO date string. Defaults to today.

    Returns:
        Report dict conforming to report.v1 schema.
    """
    if report_date is None:
        report_date = date.today().isoformat()

    zones_visited = summary.get("zones_visited", [])
    zone_labels = [z.get("label", "Unknown") for z in zones_visited]

    # Determine confidence from the summary
    confidence = summary.get("confidence", "low")
    if confidence not in ("high", "medium", "low", "needs_review"):
        confidence = "low"

    gaps = []
    for g in summary.get("major_gaps", []):
        gaps.append(f"Gap in location data: {g}")

    narrative = summary.get("narrative", "No location data available.")

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "location",
        "period_start": report_date,
        "period_end": report_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "confidence": confidence,
        "summary": narrative,
        "items": zones_visited,
        "gaps": gaps,
        "privacy_level": "safe_for_user_dm",
        "first_seen": summary.get("first_seen"),
        "last_seen": summary.get("last_seen"),
        "zone_labels": zone_labels,
    }


def render_location_markdown(report: dict[str, Any]) -> str:
    """Render a location report as Markdown."""
    lines = [
        f"# Location Report — {report.get('period_start', '')}",
        "",
        f"**Summary:** {report.get('summary', 'No data')}",
        f"**Confidence:** {report.get('confidence', 'unknown')}",
        "",
    ]

    first = report.get("first_seen")
    last = report.get("last_seen")
    if first and last:
        lines.append(f"**Active window:** {first} → {last}")
        lines.append("")

    for item in report.get("items", []):
        lines.append(
            f"- **{item.get('label', 'Unknown')}**: {item.get('time_range', 'unknown duration')}"
        )

    gaps = report.get("gaps", [])
    if gaps:
        lines.append("")
        lines.append("### Gaps")
        for g in gaps:
            lines.append(f"- {g}")

    return "\n".join(lines) + "\n"


def write_location_report(
    summary: dict[str, Any],
    data_dir: Path | str | None = None,
    report_date: Optional[str] = None,
) -> Path:
    """Build and write location report (JSON + Markdown)."""
    report = build_location_report(summary, report_date)
    date_str = report_date or date.today().isoformat()
    json_path = write_report_json(report, "location", date_str, data_dir)
    write_report_markdown(report, "location", date_str, data_dir)
    return json_path