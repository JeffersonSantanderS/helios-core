"""Helios v6 — Monthly report generation with Obsidian export (SAN-123).

Produces a monthly summary report from the Helios database with trend
analysis and writes it to:
  - JSON/Markdown via the report.v1 helpers
  - Obsidian vault at {vault_path}/Helios/Monthly/YYYY-MM.md

Sections: Sleep Trends, Activity Trends, Mood Patterns, Focus Patterns,
Top Music, Subscription Costs, Health Highlights.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import REPORT_SCHEMA_VERSION, write_report_json, write_report_markdown
from .weekly import (
    _avg,
    _query_calendar_events,
    _query_focus_range,
    _query_metric_range,
    _query_mood_range,
    _query_spotify_range,
    _query_subscriptions,
    _resolve_vault_path,
    _trend,
    HEALTH_METRICS,
    MOOD_METRIC,
    PHONE_METRICS,
)

logger = logging.getLogger("helios.reports.monthly")

# ── Extended trend analysis ──────────────────────────────────────────────


def _trend_with_details(values: list[float]) -> dict[str, Any]:
    """Return a trend dict with direction, magnitude, and change percentage.

    Compares the last third of values to the first third.
    """
    if len(values) < 3:
        return {"direction": "stable", "change_pct": 0.0, "first_avg": None, "last_avg": None}

    third = max(1, len(values) // 3)
    first_third = values[:third]
    last_third = values[-third:]
    avg_first = sum(first_third) / len(first_third)
    avg_last = sum(last_third) / len(last_third)

    if avg_first == 0:
        change_pct = 0.0
        direction = "stable"
    else:
        change_pct = round(((avg_last - avg_first) / abs(avg_first)) * 100, 1)
        if change_pct > 5:
            direction = "up"
        elif change_pct < -5:
            direction = "down"
        else:
            direction = "stable"

    return {
        "direction": direction,
        "change_pct": change_pct,
        "first_avg": round(avg_first, 2),
        "last_avg": round(avg_last, 2),
    }


def _monthly_sleep_breakdown(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Build a detailed monthly sleep section with weekly breakdowns."""
    sleep_rows = _query_metric_range(
        conn, HEALTH_METRICS["sleep_avg_hours"],
        start_date, end_date,
    )
    if not sleep_rows:
        return {"available": False, "message": "No data available"}

    sleep_values = [r["value"] for r in sleep_rows]
    trend = _trend_with_details(sleep_values)

    # Weekly breakdown
    weekly_breakdown: list[dict[str, Any]] = []
    start = date.fromisoformat(start_date)
    week_num = 1
    while start <= date.fromisoformat(end_date):
        week_end = min(start + timedelta(days=6), date.fromisoformat(end_date))
        week_rows = [
            r for r in sleep_rows
            if start.isoformat() <= r["date_key"] <= week_end.isoformat()
        ]
        if week_rows:
            week_values = [r["value"] for r in week_rows]
            weekly_breakdown.append({
                "week": week_num,
                "start": start.isoformat(),
                "end": week_end.isoformat(),
                "average": _avg(week_values),
                "days": len(week_values),
            })
        start = week_end + timedelta(days=1)
        week_num += 1

    # Deep sleep and REM averages
    deep_rows = _query_metric_range(
        conn, HEALTH_METRICS["sleep_deep_hours"], start_date, end_date,
    )
    rem_rows = _query_metric_range(
        conn, HEALTH_METRICS["sleep_rem_hours"], start_date, end_date,
    )
    hr_rows = _query_metric_range(
        conn, HEALTH_METRICS["resting_hr"], start_date, end_date,
    )
    hrv_rows = _query_metric_range(
        conn, HEALTH_METRICS["hrv_ms"], start_date, end_date,
    )

    result: dict[str, Any] = {
        "available": True,
        "average_hours": _avg(sleep_values),
        "total_days": len(sleep_values),
        "trend": trend,
        "best_night": max(sleep_rows, key=lambda r: r["value"]),
        "worst_night": min(sleep_rows, key=lambda r: r["value"]),
        "weekly_breakdown": weekly_breakdown,
    }

    if deep_rows:
        result["deep_avg_hours"] = _avg([r["value"] for r in deep_rows])
    if rem_rows:
        result["rem_avg_hours"] = _avg([r["value"] for r in rem_rows])

    # Health highlights (resting HR, HRV)
    highlights: dict[str, Any] = {}
    if hr_rows:
        hr_values = [r["value"] for r in hr_rows]
        highlights["resting_hr_avg"] = _avg(hr_values)
        highlights["resting_hr_trend"] = _trend_with_details(hr_values)
    if hrv_rows:
        hrv_values = [r["value"] for r in hrv_rows]
        highlights["hrv_avg"] = _avg(hrv_values)
        highlights["hrv_trend"] = _trend_with_details(hrv_values)

    result["health_highlights"] = highlights
    return result


def _monthly_activity_breakdown(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Build a detailed monthly activity section with weekly breakdowns."""
    steps_rows = _query_metric_range(
        conn, HEALTH_METRICS["steps_daily"],
        start_date, end_date,
        source="home_assistant_health",
    )
    if not steps_rows:
        steps_rows = _query_metric_range(
            conn, PHONE_METRICS["phone_steps"],
            start_date, end_date,
        )

    active_rows = _query_metric_range(
        conn, HEALTH_METRICS["active_minutes"],
        start_date, end_date,
    )
    walking_rows = _query_metric_range(
        conn, HEALTH_METRICS["walking_km"],
        start_date, end_date,
    )

    if not steps_rows and not active_rows:
        return {"available": False, "message": "No data available"}

    result: dict[str, Any] = {"available": True}

    if steps_rows:
        step_values = [r["value"] for r in steps_rows]
        result["steps"] = {
            "total": int(sum(step_values)),
            "average": _avg(step_values),
            "days_tracked": len(step_values),
            "trend": _trend_with_details(step_values),
        }

    if active_rows:
        active_values = [r["value"] for r in active_rows]
        result["active_minutes"] = {
            "total": int(sum(active_values)),
            "average": _avg(active_values),
            "days_tracked": len(active_values),
            "trend": _trend_with_details(active_values),
        }

    if walking_rows:
        walk_values = [r["value"] for r in walking_rows]
        result["walking_km"] = {
            "total": round(sum(walk_values), 1),
            "average": _avg(walk_values),
            "days_tracked": len(walk_values),
        }

    return result


def _monthly_mood_breakdown(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Build a detailed monthly mood section with weekly breakdowns."""
    mood_rows = _query_mood_range(conn, start_date, end_date)

    if not mood_rows:
        # Try metric_snapshots as fallback
        mood_metric_rows = _query_metric_range(
            conn, MOOD_METRIC, start_date, end_date,
        )
        if mood_metric_rows:
            values = [r["value"] for r in mood_metric_rows]
            return {
                "available": True,
                "average": _avg(values),
                "days_tracked": len(values),
                "trend": _trend_with_details(values),
                "source": "metric_snapshots",
            }
        return {"available": False, "message": "No data available"}

    avg_scores = [r["avg_score"] for r in mood_rows if r["avg_score"] is not None]
    return {
        "available": True,
        "average": _avg(avg_scores) if avg_scores else None,
        "days_tracked": len(mood_rows),
        "trend": _trend_with_details(avg_scores) if len(avg_scores) >= 3 else {"direction": "stable", "change_pct": 0.0},
        "source": "mood_table",
    }


def _monthly_focus_breakdown(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Build a detailed monthly focus section with weekly breakdowns."""
    focus_rows = _query_focus_range(conn, start_date, end_date)
    screen_rows = _query_metric_range(
        conn, PHONE_METRICS["screen_time_minutes"],
        start_date, end_date,
    )

    result: dict[str, Any] = {"available": False}

    if focus_rows:
        working_rows = [r for r in focus_rows if r["state"] == "working"]
        if working_rows:
            total_hours = sum(r["total_seconds"] for r in working_rows) / 3600
            result["focus"] = {
                "total_hours": round(total_hours, 1),
                "total_sessions": sum(r["session_count"] for r in working_rows),
                "days_with_focus": len({r["date"] for r in working_rows}),
            }

            # Weekly breakdown of focus time
            weekly_focus: dict[str, float] = {}
            for r in working_rows:
                week_key = r["date"][:7]  # Group by week — simplified
                weekly_focus[week_key] = weekly_focus.get(week_key, 0) + r["total_seconds"] / 3600
            result["focus"]["weekly_breakdown"] = [
                {"week": wk, "hours": round(hrs, 1)}
                for wk, hrs in sorted(weekly_focus.items())
            ]
            result["available"] = True

    if screen_rows:
        screen_values = [r["value"] for r in screen_rows]
        result["screen_time"] = {
            "average_daily_minutes": _avg(screen_values),
            "total_minutes": int(sum(screen_values)),
            "days_tracked": len(screen_values),
            "trend": _trend_with_details(screen_values),
        }
        result["available"] = True

    if not result["available"]:
        result["message"] = "No data available"

    return result


# ── MonthlyReport class ──────────────────────────────────────────────────


class MonthlyReport:
    """Generate a monthly summary report from the Helios database.

    Parameters
    ----------
    db_path:
        Path to the Helios SQLite database.
    year:
        Year for the report (defaults to current year).
    month:
        Month for the report (1-12, defaults to current month).
    vault_path:
        Optional Obsidian vault path for writing.
    """

    encrypted_state = False  # Reports are not sensitive

    def __init__(
        self,
        db_path: str,
        year: Optional[int] = None,
        month: Optional[int] = None,
        vault_path: Optional[str] = None,
    ) -> None:
        self.db_path = db_path
        now = datetime.now(timezone.utc)
        self.year = year or now.year
        self.month = month or now.month
        self.start_date = date(self.year, self.month, 1)
        # End date is last day of the month
        if self.month == 12:
            self.end_date = date(self.year + 1, 1, 1) - timedelta(days=1)
        else:
            self.end_date = date(self.year, self.month + 1, 1) - timedelta(days=1)
        self.vault_path = _resolve_vault_path(vault_path)

    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Build report dict ──────────────────────────────────────────────

    def build(self) -> dict[str, Any]:
        """Build the full monthly report dict from database queries."""
        conn = self._get_conn()
        try:
            sleep = _monthly_sleep_breakdown(
                conn, self.start_date.isoformat(), self.end_date.isoformat(),
            )
            activity = _monthly_activity_breakdown(
                conn, self.start_date.isoformat(), self.end_date.isoformat(),
            )
            mood = _monthly_mood_breakdown(
                conn, self.start_date.isoformat(), self.end_date.isoformat(),
            )
            focus = _monthly_focus_breakdown(
                conn, self.start_date.isoformat(), self.end_date.isoformat(),
            )
            music = self._build_music(conn)
            subscriptions = self._build_subscriptions(conn)
            health_highlights = self._build_health_highlights(conn)
        finally:
            conn.close()

        label = f"{self.year}-{self.month:02d}"

        # Build narrative summary
        summary_parts = []
        if sleep.get("available") and sleep.get("average_hours") is not None:
            summary_parts.append(f"avg {sleep['average_hours']:.1f}h sleep")
        if activity.get("available") and "steps" in activity:
            summary_parts.append(f"{activity['steps']['total']:,} total steps")
        if mood.get("available") and mood.get("average") is not None:
            summary_parts.append(f"mood {mood['average']:.1f}/10")
        if focus.get("available") and "focus" in focus:
            summary_parts.append(f"{focus['focus']['total_hours']:.1f}h focus time")

        summary = "; ".join(summary_parts) + "." if summary_parts else "No data available for this month."

        sections = [
            {"key": "sleep", "label": "Sleep Trends", "data": sleep},
            {"key": "activity", "label": "Activity Trends", "data": activity},
            {"key": "mood", "label": "Mood Patterns", "data": mood},
            {"key": "focus", "label": "Focus Patterns", "data": focus},
            {"key": "music", "label": "Top Music", "data": music},
            {"key": "subscriptions", "label": "Subscription Costs", "data": subscriptions},
            {"key": "health_highlights", "label": "Health Highlights", "data": health_highlights},
        ]

        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_type": "monthly",
            "period_start": self.start_date.isoformat(),
            "period_end": self.end_date.isoformat(),
            "month_label": label,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "confidence": self._determine_confidence(sleep, activity, mood, focus),
            "summary": summary,
            "sections": sections,
            "privacy_level": "safe_for_user_dm",
            "encrypted_state": False,
        }

    def _build_music(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Build top music section from spotify data."""
        spotify_rows = _query_spotify_range(
            conn, self.start_date.isoformat(), self.end_date.isoformat(),
        )
        if not spotify_rows:
            return {"available": False, "message": "No data available"}

        artists: dict[str, int] = {}
        tracks: dict[str, int] = {}
        total_minutes = 0.0

        for row in spotify_rows:
            try:
                value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(value, dict):
                continue
            if "artist" in value:
                artists[value["artist"]] = artists.get(value["artist"], 0) + 1
            if "track" in value:
                tracks[value["track"]] = tracks.get(value["track"], 0) + 1
            if "total_minutes" in value:
                total_minutes += value["total_minutes"]

        top_artists = sorted(artists.items(), key=lambda x: x[1], reverse=True)[:10]
        top_tracks = sorted(tracks.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            "available": True,
            "total_minutes": round(total_minutes, 1) if total_minutes else None,
            "top_artists": [{"name": a[0], "plays": a[1]} for a in top_artists],
            "top_tracks": [{"name": t[0], "plays": t[1]} for t in top_tracks],
            "listening_days": len({row["ts"][:10] for row in spotify_rows}),
        }

    def _build_subscriptions(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Build subscription costs section."""
        subs = _query_subscriptions(conn)
        if not subs:
            return {"available": False, "message": "No data available"}

        monthly_total = sum(
            s["amount"] for s in subs
            if s.get("cycle") == "monthly" and s.get("amount")
        )
        yearly_total = sum(
            s["amount"] for s in subs
            if s.get("cycle") == "yearly" and s.get("amount")
        )
        weekly_total = sum(
            s["amount"] for s in subs
            if s.get("cycle") == "weekly" and s.get("amount")
        )

        # Normalize all to estimated monthly cost
        estimated_monthly = round(
            monthly_total + (yearly_total / 12) + (weekly_total * 4.33),
            2,
        )

        # Group by category
        categories: dict[str, list[dict]] = {}
        for s in subs:
            cat = s.get("category", "other")
            categories.setdefault(cat, []).append({
                "service": s["service"],
                "amount": s["amount"],
                "cycle": s.get("cycle", "monthly"),
                "next_renewal": s.get("next_renewal"),
            })

        # Upcoming renewals this month
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month_end_str = self.end_date.isoformat()
        upcoming = [
            {
                "service": s["service"],
                "amount": s["amount"],
                "currency": s.get("currency", "CAD"),
                "cycle": s.get("cycle", "monthly"),
                "next_renewal": s.get("next_renewal"),
                "category": s.get("category", "other"),
            }
            for s in subs
            if s.get("next_renewal") and today_str <= s["next_renewal"] <= month_end_str
        ]

        return {
            "available": True,
            "total_active": len(subs),
            "monthly_cost": round(monthly_total, 2),
            "yearly_cost": round(yearly_total, 2),
            "weekly_cost": round(weekly_total, 2),
            "estimated_monthly_total": estimated_monthly,
            "categories": categories,
            "upcoming_this_month": upcoming,
        }

    def _build_health_highlights(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Build health highlights section (HRV, resting HR, respiratory, O2)."""
        highlights: dict[str, Any] = {}

        hr_rows = _query_metric_range(
            conn, HEALTH_METRICS["resting_hr"],
            self.start_date.isoformat(), self.end_date.isoformat(),
        )
        hrv_rows = _query_metric_range(
            conn, HEALTH_METRICS["hrv_ms"],
            self.start_date.isoformat(), self.end_date.isoformat(),
        )

        if hr_rows:
            hr_values = [r["value"] for r in hr_rows]
            highlights["resting_hr"] = {
                "average": _avg(hr_values),
                "min": min(hr_values),
                "max": max(hr_values),
                "trend": _trend_with_details(hr_values),
            }

        if hrv_rows:
            hrv_values = [r["value"] for r in hrv_rows]
            highlights["hrv"] = {
                "average": _avg(hrv_values),
                "min": min(hrv_values),
                "max": max(hrv_values),
                "trend": _trend_with_details(hrv_values),
            }

        if not highlights:
            return {"available": False, "message": "No data available"}

        highlights["available"] = True
        return highlights

    def _determine_confidence(
        self,
        sleep: dict,
        activity: dict,
        mood: dict,
        focus: dict,
    ) -> str:
        """Determine overall confidence based on available sections."""
        available = sum(
            1 for s in [sleep, activity, mood, focus] if s.get("available")
        )
        if available >= 3:
            return "high"
        if available >= 2:
            return "medium"
        if available >= 1:
            return "low"
        return "needs_review"

    # ── Markdown rendering ─────────────────────────────────────────────

    def render_markdown(self, data: Optional[dict[str, Any]] = None) -> str:
        """Render the monthly report as Obsidian-compatible Markdown."""
        if data is None:
            data = self.build()
        lines: list[str] = []

        label = data.get("month_label", "Monthly")
        start = data.get("period_start", "")
        end = data.get("period_end", "")
        lines.append(f"# 📅 Monthly Report — {label}")
        lines.append("")
        lines.append(f"**Period**: {start} → {end}")
        lines.append(f"**Generated**: {data.get('generated_at', 'unknown')}")
        lines.append(f"**Confidence**: {data.get('confidence', 'unknown')}")
        lines.append(f"> {data.get('summary', 'No data available')}")
        lines.append("")

        for section in data.get("sections", []):
            key = section.get("key", "")
            label = section.get("label", key)
            section_data = section.get("data", {})
            available = section_data.get("available", False)

            lines.append(f"## {label}")
            lines.append("")

            if not available:
                lines.append(f"*{section_data.get('message', 'No data available')}*")
                lines.append("")
                continue

            if key == "sleep":
                self._render_sleep_md(lines, section_data)
            elif key == "activity":
                self._render_activity_md(lines, section_data)
            elif key == "mood":
                self._render_mood_md(lines, section_data)
            elif key == "focus":
                self._render_focus_md(lines, section_data)
            elif key == "music":
                self._render_music_md(lines, section_data)
            elif key == "subscriptions":
                self._render_subscriptions_md(lines, section_data)
            elif key == "health_highlights":
                self._render_health_md(lines, section_data)
            else:
                for k, v in section_data.items():
                    if k not in ("available", "message"):
                        lines.append(f"- **{k}**: {v}")
                lines.append("")

        lines.append("---")
        lines.append(f"*Helios v6 — Monthly Report • {data.get('generated_at', 'unknown')}*")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _render_sleep_md(lines: list[str], data: dict) -> None:
        avg = data.get("average_hours")
        if avg is not None:
            lines.append(f"- **Average sleep**: {avg:.1f}h")
            lines.append(f"- **Days tracked**: {data.get('total_days', 0)}")
            trend = data.get("trend", {})
            if isinstance(trend, dict):
                direction = trend.get("direction", "stable")
                change = trend.get("change_pct", 0)
                trend_icon = {"up": "📈", "down": "📉", "stable": "➡️"}.get(direction, "➡️")
                lines.append(f"- **Trend**: {trend_icon} {direction} ({change:+.1f}%)")
            best = data.get("best_night")
            if best:
                lines.append(f"- **Best night**: {best.get('date_key', '?')} ({best.get('value', '?')}h)")
            worst = data.get("worst_night")
            if worst:
                lines.append(f"- **Worst night**: {worst.get('date_key', '?')} ({worst.get('value', '?')}h)")
            if "deep_avg_hours" in data:
                lines.append(f"- **Deep sleep avg**: {data['deep_avg_hours']:.1f}h")
            if "rem_avg_hours" in data:
                lines.append(f"- **REM sleep avg**: {data['rem_avg_hours']:.1f}h")
            weekly = data.get("weekly_breakdown", [])
            if weekly:
                lines.append("")
                lines.append("| Week | Period | Avg | Days |")
                lines.append("|------|--------|-----|------|")
                for w in weekly:
                    lines.append(f"| {w['week']} | {w['start']} → {w['end']} | {w['average']:.1f}h | {w['days']} |")
        lines.append("")

    @staticmethod
    def _render_activity_md(lines: list[str], data: dict) -> None:
        steps = data.get("steps")
        if steps:
            lines.append(f"- **Total steps**: {steps.get('total', 0):,}")
            lines.append(f"- **Average daily**: {steps.get('average', 0):,.0f}")
            trend = steps.get("trend", {})
            if isinstance(trend, dict):
                direction = trend.get("direction", "stable")
                change = trend.get("change_pct", 0)
                trend_icon = {"up": "📈", "down": "📉", "stable": "➡️"}.get(direction, "➡️")
                lines.append(f"- **Trend**: {trend_icon} {direction} ({change:+.1f}%)")
        active = data.get("active_minutes")
        if active:
            lines.append(f"- **Active minutes**: {active.get('total', 0):,} total ({active.get('average', 0):.0f} avg/day)")
        walking = data.get("walking_km")
        if walking:
            lines.append(f"- **Walking distance**: {walking.get('total', 0):.1f} km total")
        lines.append("")

    @staticmethod
    def _render_mood_md(lines: list[str], data: dict) -> None:
        avg = data.get("average")
        if avg is not None:
            trend = data.get("trend", {})
            if isinstance(trend, dict):
                direction = trend.get("direction", "stable")
                change = trend.get("change_pct", 0)
                trend_icon = {"up": "📈", "down": "📉", "stable": "➡️"}.get(direction, "➡️")
                lines.append(f"- **Average mood**: {avg:.1f}/10 {trend_icon} {direction} ({change:+.1f}%)")
            else:
                lines.append(f"- **Average mood**: {avg:.1f}/10")
            lines.append(f"- **Days tracked**: {data.get('days_tracked', 0)}")
        lines.append("")

    @staticmethod
    def _render_focus_md(lines: list[str], data: dict) -> None:
        focus = data.get("focus")
        if focus:
            lines.append(f"- **Total focus time**: {focus.get('total_hours', 0):.1f}h")
            lines.append(f"- **Focus sessions**: {focus.get('total_sessions', 0)}")
            lines.append(f"- **Days with focus**: {focus.get('days_with_focus', 0)}")
            weekly = focus.get("weekly_breakdown", [])
            if weekly:
                lines.append("")
                lines.append("| Week | Hours |")
                lines.append("|------|-------|")
                for w in weekly:
                    lines.append(f"| {w['week']} | {w['hours']:.1f}h |")
        screen = data.get("screen_time")
        if screen:
            lines.append(f"- **Screen time avg**: {screen.get('average_daily_minutes', 0):.0f} min/day")
            trend = screen.get("trend", {})
            if isinstance(trend, dict):
                direction = trend.get("direction", "stable")
                change = trend.get("change_pct", 0)
                trend_icon = {"up": "📈", "down": "📉", "stable": "➡️"}.get(direction, "➡️")
                lines.append(f"- **Screen time trend**: {trend_icon} {direction} ({change:+.1f}%)")
        lines.append("")

    @staticmethod
    def _render_music_md(lines: list[str], data: dict) -> None:
        if data.get("total_minutes"):
            lines.append(f"- **Total listening**: {data['total_minutes']:.0f} min")
        if data.get("listening_days"):
            lines.append(f"- **Listening days**: {data['listening_days']}")
        artists = data.get("top_artists", [])
        if artists:
            lines.append("")
            lines.append("**Top Artists**:")
            for i, a in enumerate(artists[:5], 1):
                lines.append(f"{i}. {a['name']} ({a['plays']} plays)")
        tracks = data.get("top_tracks", [])
        if tracks:
            lines.append("")
            lines.append("**Top Tracks**:")
            for i, t in enumerate(tracks[:5], 1):
                lines.append(f"{i}. {t['name']} ({t['plays']} plays)")
        lines.append("")

    @staticmethod
    def _render_subscriptions_md(lines: list[str], data: dict) -> None:
        lines.append(f"- **Active subscriptions**: {data.get('total_active', 0)}")
        lines.append(f"- **Monthly cost**: ${data.get('monthly_cost', 0):.2f} CAD")
        lines.append(f"- **Yearly cost**: ${data.get('yearly_cost', 0):.2f} CAD")
        lines.append(f"- **Estimated monthly total**: ${data.get('estimated_monthly_total', 0):.2f} CAD")
        categories = data.get("categories", {})
        if categories:
            lines.append("")
            lines.append("**By Category**:")
            for cat, subs in categories.items():
                cat_total = sum(s["amount"] for s in subs)
                lines.append(f"- **{cat}** ({len(subs)}): ${cat_total:.2f}")
                for s in subs:
                    lines.append(f"  - {s['service']}: ${s['amount']:.2f}/{s['cycle']}")
        upcoming = data.get("upcoming_this_month", [])
        if upcoming:
            lines.append("")
            lines.append("**Renewing This Month**:")
            for s in upcoming:
                lines.append(f"- {s['service']}: ${s['amount']:.2f} ({s['cycle']}) — {s.get('next_renewal', '?')}")
        lines.append("")

    @staticmethod
    def _render_health_md(lines: list[str], data: dict) -> None:
        if "resting_hr" in data:
            hr = data["resting_hr"]
            avg = hr.get("average")
            if avg is not None:
                lines.append(f"- **Resting HR avg**: {avg:.0f} bpm (range: {hr.get('min', '?')}–{hr.get('max', '?')})")
                trend = hr.get("trend", {})
                if isinstance(trend, dict):
                    direction = trend.get("direction", "stable")
                    change = trend.get("change_pct", 0)
                    trend_icon = {"up": "📈", "down": "📉", "stable": "➡️"}.get(direction, "➡️")
                    lines.append(f"  - Trend: {trend_icon} {direction} ({change:+.1f}%)")
        if "hrv" in data:
            hrv = data["hrv"]
            avg = hrv.get("average")
            if avg is not None:
                lines.append(f"- **HRV avg**: {avg:.0f} ms (range: {hrv.get('min', '?')}–{hrv.get('max', '?')})")
                trend = hrv.get("trend", {})
                if isinstance(trend, dict):
                    direction = trend.get("direction", "stable")
                    change = trend.get("change_pct", 0)
                    trend_icon = {"up": "📈", "down": "📉", "stable": "➡️"}.get(direction, "➡️")
                    lines.append(f"  - Trend: {trend_icon} {direction} ({change:+.1f}%)")
        lines.append("")

    # ── File writing ────────────────────────────────────────────────────

    def generate(self) -> tuple[str, Optional[Path]]:
        """Generate the monthly report: return markdown and write to Obsidian.

        Returns
        -------
        (markdown, obsidian_path)
            markdown: The rendered Markdown string.
            obsidian_path: Path to the written Obsidian file, or None
                           if vault is not configured.
        """
        data = self.build()
        md = self.render_markdown(data)

        label = f"{self.year}-{self.month:02d}"

        try:
            write_report_json(data, "monthly", label)
            write_report_markdown(data, "monthly", label)
        except Exception as exc:
            logger.warning("Failed to write report files: %s", exc)

        # Write to Obsidian vault
        obsidian_path: Optional[Path] = None
        if self.vault_path:
            try:
                monthly_dir = self.vault_path / "Helios" / "Monthly"
                monthly_dir.mkdir(parents=True, exist_ok=True)
                filename = f"{label}.md"
                obsidian_path = monthly_dir / filename
                obsidian_path.write_text(md, encoding="utf-8")
                logger.info("Monthly report written to Obsidian: %s", obsidian_path)
            except Exception as exc:
                logger.error("Failed to write monthly report to Obsidian: %s", exc)

        return md, obsidian_path


# ── Standalone Markdown rendering (for report.v1 dispatch) ─────────────

def render_monthly_markdown(data: dict[str, Any]) -> str:
    """Render a monthly report dict to Markdown (for report.v1 dispatch)."""
    report = MonthlyReport.__new__(MonthlyReport)
    return MonthlyReport.render_markdown(report, data)