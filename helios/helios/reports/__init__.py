"""Helios v7 — Report generation package.

Provides helpers for writing health diary reports (and future report types)
as JSON and Markdown, using atomic (temp+rename) writes.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("helios.reports")

REPORT_SCHEMA_VERSION = "report.v1"

DEFAULT_DATA_DIR = Path.home() / ".hermes" / "helios" / "data" / "reports"


def report_filename(report_type: str, date_str: str) -> str:
    """Return the base filename (without extension) for a report.

    Format: ``{report_type}_{date_str}``

    >>> report_filename("health_diary", "2025-05-25")
    'health_diary_2025-05-25'
    """
    return f"{report_type}_{date_str}"


def write_report_json(
    data: dict[str, Any],
    report_type: str,
    date_str: str,
    data_dir: Path | str | None = None,
) -> Path:
    """Write a report dict as JSON using atomic temp+rename.

    Args:
        data:        The report payload (must include ``schema_version``).
        report_type: e.g. ``"health_diary"``.
        date_str:    ISO date string ``YYYY-MM-DD``.
        data_dir:    Override destination directory. Defaults to
                     ``~/.hermes/helios/data/reports/``.

    Returns:
        Path to the written JSON file.
    """
    dir_path = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    dir_path.mkdir(parents=True, exist_ok=True)
    filename = report_filename(report_type, date_str) + ".json"
    target = dir_path / filename
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(target)
    logger.info("Report JSON written: %s", target)
    return target


def write_report_markdown(
    data: dict[str, Any],
    report_type: str,
    date_str: str,
    data_dir: Path | str | None = None,
) -> Path:
    """Write a Markdown version of the report next to the JSON.

    The Markdown is generated from ``data`` using
    :func:`helios.reports.health.render_markdown` when ``report_type`` is
    ``"health_diary"``. For unknown types a simple key-value dump is produced.

    Returns:
        Path to the written Markdown file.
    """
    dir_path = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    dir_path.mkdir(parents=True, exist_ok=True)

    if report_type == "health_diary":
        from .health import render_markdown
        md_content = render_markdown(data)
    elif report_type == "spotify_daily":
        from .spotify import render_spotify_markdown
        md_content = render_spotify_markdown(data)
    elif report_type == "work_hours":
        from .work_hours import render_work_hours_markdown
        md_content = render_work_hours_markdown(data)
    elif report_type == "location":
        from .location import render_location_markdown
        md_content = render_location_markdown(data)
    elif report_type == "agenda":
        from .agenda import render_agenda_markdown
        md_content = render_agenda_markdown(data)
    else:
        # Fallback: simple key-value dump
        lines = [f"# {report_type} Report — {date_str}", ""]
        for key, value in data.items():
            lines.append(f"- **{key}**: {value}")
        md_content = "\n".join(lines) + "\n"

    filename = report_filename(report_type, date_str) + ".md"
    target = dir_path / filename
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(md_content, encoding="utf-8")
    tmp.replace(target)
    logger.info("Report Markdown written: %s", target)
    return target