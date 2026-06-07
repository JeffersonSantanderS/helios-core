"""Helios v7 — Work hours report generation.

Reads work-hours analyzer output and produces a report.v1 JSON + Markdown
suitable for dashboard cards and query answers.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import REPORT_SCHEMA_VERSION, write_report_json, write_report_markdown

logger = logging.getLogger("helios.reports.work_hours")

DEFAULT_DATA_DIR = Path.home() / ".hermes" / "helios" / "data"


def build_work_hours_report(
    state: dict[str, Any],
    report_date: Optional[str] = None,
) -> dict[str, Any]:
    """Build a report.v1 work-hours report from analyzer state.

    Args:
        state: The work_hours_state.json dict.
        report_date: ISO date string. Defaults to today.

    Returns:
        Report dict conforming to report.v1 schema.
    """
    if report_date is None:
        report_date = date.today().isoformat()

    days = state.get("days", [])
    review = state.get("review", {})

    # Calculate summary
    total_hours = sum(d.get("paid_hours", 0.0) for d in days if d.get("paid_hours"))
    high_conf = sum(1 for d in days if d.get("confidence") == "high")
    med_conf = sum(1 for d in days if d.get("confidence") == "medium")
    low_conf = sum(1 for d in days if d.get("confidence") in ("low", "needs_review"))

    confidence = "high"
    if review.get("needs_review_count", 0) > 0 or low_conf > 2:
        confidence = "needs_review"
    elif low_conf > 0 or med_conf > 1:
        confidence = "medium"

    items = []
    for d in days:
        items.append({
            "date": d.get("date", ""),
            "line": d.get("line", ""),
            "paid_hours": d.get("paid_hours", 0.0),
            "confidence": d.get("confidence", "unknown"),
            "source": d.get("source", "unknown"),
            "confidence_reason": d.get("confidence_reason", ""),
            "evidence_summary": d.get("evidence_summary", ""),
        })

    gaps = []
    for md in review.get("missing_days", []):
        gaps.append(f"Missing data for {md}")

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "work_hours",
        "period_start": state.get("period_start", report_date),
        "period_end": state.get("period_end", report_date),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "confidence": confidence,
        "summary": state.get("report_text", f"Pay period: {total_hours:.1f} hours"),
        "items": items,
        "gaps": gaps,
        "privacy_level": "safe_for_user_dm",
        "total_paid_hours": total_hours,
        "review": review,
        "copy_paste_ready": review.get("copy_paste_ready", False),
    }


def render_work_hours_markdown(report: dict[str, Any]) -> str:
    """Render a work-hours report as Markdown."""
    lines = [
        f"# Work Hours Report — {report.get('period_start', '')} to {report.get('period_end', '')}",
        "",
        f"**Total hours:** {report.get('total_paid_hours', 0.0):.1f}",
        f"**Confidence:** {report.get('confidence', 'unknown')}",
        f"**Copy-paste ready:** {'Yes' if report.get('copy_paste_ready') else 'No'}",
        "",
    ]

    review = report.get("review", {})
    if review.get("needs_review_count", 0) > 0:
        lines.append(f"⚠ **Days needing review:** {review['needs_review_count']}")
    if review.get("manual_override_count", 0) > 0:
        lines.append(f"📋 **Manual overrides:** {review['manual_override_count']}")
    lines.append("")

    for item in report.get("items", []):
        conf = item.get("confidence", "unknown")
        src = item.get("source", "")
        lines.append(f"- **{item.get('date', '')}**: {item.get('line', '')} "
                      f"({conf}, {src})")

    gaps = report.get("gaps", [])
    if gaps:
        lines.append("")
        lines.append("### Gaps")
        for g in gaps:
            lines.append(f"- {g}")

    return "\n".join(lines) + "\n"


def write_work_hours_report(
    state: dict[str, Any],
    data_dir: Path | str | None = None,
    report_date: Optional[str] = None,
) -> Path:
    """Build and write work-hours report (JSON + Markdown).

    Returns:
        Path to the written JSON file.
    """
    report = build_work_hours_report(state, report_date)
    date_str = report_date or date.today().isoformat()
    json_path = write_report_json(report, "work_hours", date_str, data_dir)
    write_report_markdown(report, "work_hours", date_str, data_dir)
    return json_path