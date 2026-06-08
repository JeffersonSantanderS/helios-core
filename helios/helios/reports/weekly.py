"""Helios v6 — Weekly report generation with Obsidian export (SAN-123).

Produces a weekly summary report from the Helios database (metric_snapshots,
mood, focus, subscriptions) and writes it to:
  - JSON/Markdown via the report.v1 helpers
  - Obsidian vault at {vault_path}/Helios/Weekly/YYYY-WNN.md

Sections: Sleep Summary, Steps & Activity, Mood, Focus & Screen Time,
Top Music, Calendar Events, Upcoming Renewals.
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

logger = logging.getLogger("helios.reports.weekly")

# ── Default vault path resolution ──────────────────────────────────────

DEFAULT_HELIOS_BASE = os.path.join(os.path.expanduser("~"), ".hermes", "helios")


def _resolve_vault_path(vault_path: Optional[str] = None) -> Optional[Path]:
    """Resolve the Obsidian vault path from env, arg, or default.

    Resolution order:
      1. Explicit vault_path argument
      2. HELIOS_OBSIDIAN_VAULT env var
      3. HELIOS_BASE/../obsidian (sibling of helios data dir)
      4. None (vault writing disabled)
    """
    if vault_path:
        return Path(vault_path)

    env_vault = os.environ.get("HELIOS_OBSIDIAN_VAULT")
    if env_vault:
        return Path(env_vault)

    # Try to derive from HELIOS_BASE
    base = os.environ.get("HELIOS_BASE", DEFAULT_HELIOS_BASE)
    candidate = Path(base).parent / "obsidian"
    if candidate.is_dir():
        return candidate

    return None


# ── Metric query helpers ─────────────────────────────────────────────────

# Map of canonical metric names to the metric_snapshots.metric values.
HEALTH_METRICS = {
    "sleep_avg_hours": "sleep.hours",
    "sleep_deep_hours": "sleep.deep_hours",
    "sleep_rem_hours": "sleep.rem_hours",
    "sleep_core_hours": "sleep.core_hours",
    "resting_hr": "health.resting_hr",
    "hrv_ms": "health.hrv_ms",
    "steps_daily": "activity.steps_daily",
    "active_minutes": "activity.minutes_daily",
    "active_energy": "activity.active_energy_kj",
    "walking_km": "activity.walking_km",
    "stand_hours": "activity.stand_hours",
}

PHONE_METRICS = {
    "phone_steps": "phone.steps_daily",
    "screen_time_minutes": "phone.screen_time_minutes",
    "screen_time_pickups": "phone.screen_time_pickups",
}

MOOD_METRIC = "mood.score_daily"


def _query_metric_range(
    conn: sqlite3.Connection,
    metric: str,
    start_date: str,
    end_date: str,
    source: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Query metric_snapshots for a given metric within a date range.

    Returns a list of dicts with keys: date_key, value.
    """
    if source:
        rows = conn.execute(
            """SELECT date_key, value FROM metric_snapshots
               WHERE metric = ? AND date_key >= ? AND date_key <= ? AND source = ?
               ORDER BY date_key""",
            (metric, start_date, end_date, source),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT date_key, value FROM metric_snapshots
               WHERE metric = ? AND date_key >= ? AND date_key <= ?
               ORDER BY date_key""",
            (metric, start_date, end_date),
        ).fetchall()
    return [{"date_key": r[0], "value": r[1]} for r in rows]


def _query_mood_range(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Query mood table for scores within a date range."""
    try:
        rows = conn.execute(
            """SELECT date(ts) as day, AVG(score) as avg_score, COUNT(*) as count
               FROM mood
               WHERE ts >= ? AND ts <= ?
               GROUP BY day
               ORDER BY day""",
            (f"{start_date}T00:00:00Z", f"{end_date}T23:59:59Z"),
        ).fetchall()
        return [{"date": r[0], "avg_score": r[1], "count": r[2]} for r in rows]
    except sqlite3.OperationalError:
        return []


def _query_focus_range(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Query focus table for working sessions within a date range."""
    try:
        rows = conn.execute(
            """SELECT date(ts) as day, state,
                      SUM(COALESCE(duration_secs, 0)) as total_secs,
                      COUNT(*) as session_count
               FROM focus
               WHERE ts >= ? AND ts <= ?
               GROUP BY day, state
               ORDER BY day""",
            (f"{start_date}T00:00:00Z", f"{end_date}T23:59:59Z"),
        ).fetchall()
        return [
            {
                "date": r[0],
                "state": r[1],
                "total_seconds": r[2],
                "session_count": r[3],
            }
            for r in rows
        ]
    except sqlite3.OperationalError:
        return []


def _query_spotify_range(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Query context for spotify listening data within a date range."""
    try:
        rows = conn.execute(
            """SELECT ts, module, key, value FROM context
               WHERE module = 'spotify' AND ts >= ? AND ts <= ?
               ORDER BY ts""",
            (f"{start_date}T00:00:00Z", f"{end_date}T23:59:59Z"),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _query_calendar_events(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Query calendar_events for events within a date range."""
    try:
        rows = conn.execute(
            """SELECT title, start_time, end_time, source
               FROM calendar_events
               WHERE start_time >= ? AND start_time <= ?
               ORDER BY start_time""",
            (f"{start_date}T00:00:00Z", f"{end_date}T23:59:59Z"),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _query_subscriptions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Query active subscriptions for upcoming renewals and cost data."""
    try:
        rows = conn.execute(
            """SELECT service, amount, currency, cycle, next_renewal, category
               FROM subscriptions
               WHERE is_active = 1
               ORDER BY amount DESC""",
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


# ── Aggregation helpers ──────────────────────────────────────────────────


def _avg(values: list[float]) -> Optional[float]:
    """Return the average of a list, or None if empty."""
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _trend(values: list[float]) -> str:
    """Return a simple trend description: 'up', 'down', or 'stable'.

    Compares the second half average to the first half average.
    """
    if len(values) < 4:
        return "stable"
    mid = len(values) // 2
    first_half = values[:mid]
    second_half = values[mid:]
    avg_first = sum(first_half) / len(first_half)
    avg_second = sum(second_half) / len(second_half)
    diff_pct = ((avg_second - avg_first) / avg_first * 100) if avg_first != 0 else 0
    if diff_pct > 5:
        return "up"
    if diff_pct < -5:
        return "down"
    return "stable"


# ── WeeklyReport class ──────────────────────────────────────────────────


class WeeklyReport:
    """Generate a weekly summary report from the Helios database.

    Parameters
    ----------
    db_path:
        Path to the Helios SQLite database.
    end_date:
        The last day of the reporting week (defaults to today).
        The start date is computed as end_date - 6 days (7 days total).
    vault_path:
        Optional Obsidian vault path for writing. If not provided,
        the HELIOS_OBSIDIAN_VAULT env var is used.
    """

    encrypted_state = False  # Reports are not sensitive

    def __init__(
        self,
        db_path: str,
        end_date: Optional[date | str] = None,
        vault_path: Optional[str] = None,
    ) -> None:
        self.db_path = db_path
        if end_date is None:
            self.end_date = datetime.now(timezone.utc).date()
        elif isinstance(end_date, str):
            self.end_date = date.fromisoformat(end_date)
        else:
            self.end_date = end_date

        self.start_date = self.end_date - timedelta(days=6)
        self.vault_path = _resolve_vault_path(vault_path)

    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Section builders ──────────────────────────────────────────────

    def _build_sleep_summary(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Build sleep summary section from metric_snapshots."""
        sleep_rows = _query_metric_range(
            conn, HEALTH_METRICS["sleep_avg_hours"],
            self.start_date.isoformat(), self.end_date.isoformat(),
        )
        if not sleep_rows:
            return {"available": False, "message": "No data available"}

        sleep_values = [r["value"] for r in sleep_rows]
        deep_rows = _query_metric_range(
            conn, HEALTH_METRICS["sleep_deep_hours"],
            self.start_date.isoformat(), self.end_date.isoformat(),
        )
        rem_rows = _query_metric_range(
            conn, HEALTH_METRICS["sleep_rem_hours"],
            self.start_date.isoformat(), self.end_date.isoformat(),
        )

        result: dict[str, Any] = {
            "available": True,
            "average_hours": _avg(sleep_values),
            "total_days": len(sleep_values),
            "trend": _trend(sleep_values),
            "best_night": max(sleep_rows, key=lambda r: r["value"]),
            "worst_night": min(sleep_rows, key=lambda r: r["value"]),
        }

        if deep_rows:
            result["deep_avg_hours"] = _avg([r["value"] for r in deep_rows])
        if rem_rows:
            result["rem_avg_hours"] = _avg([r["value"] for r in rem_rows])

        return result

    def _build_steps_activity(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Build steps & activity section from metric_snapshots."""
        # Try HA health data first, then phone sensors
        steps_rows = _query_metric_range(
            conn, HEALTH_METRICS["steps_daily"],
            self.start_date.isoformat(), self.end_date.isoformat(),
            source="home_assistant_health",
        )
        if not steps_rows:
            steps_rows = _query_metric_range(
                conn, PHONE_METRICS["phone_steps"],
                self.start_date.isoformat(), self.end_date.isoformat(),
            )

        active_rows = _query_metric_range(
            conn, HEALTH_METRICS["active_minutes"],
            self.start_date.isoformat(), self.end_date.isoformat(),
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
                "best_day": max(steps_rows, key=lambda r: r["value"]),
            }

        if active_rows:
            active_values = [r["value"] for r in active_rows]
            result["active_minutes"] = {
                "total": int(sum(active_values)),
                "average": _avg(active_values),
                "days_tracked": len(active_values),
            }

        return result

    def _build_mood(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Build mood section from mood table."""
        mood_rows = _query_mood_range(
            conn, self.start_date.isoformat(), self.end_date.isoformat(),
        )
        if not mood_rows:
            # Try metric_snapshots as fallback
            mood_metric_rows = _query_metric_range(
                conn, MOOD_METRIC,
                self.start_date.isoformat(), self.end_date.isoformat(),
            )
            if mood_metric_rows:
                values = [r["value"] for r in mood_metric_rows]
                return {
                    "available": True,
                    "average": _avg(values),
                    "days_tracked": len(values),
                    "trend": _trend(values),
                    "source": "metric_snapshots",
                }
            return {"available": False, "message": "No data available"}

        avg_scores = [r["avg_score"] for r in mood_rows if r["avg_score"] is not None]
        return {
            "available": True,
            "average": _avg(avg_scores),
            "days_tracked": len(mood_rows),
            "trend": _trend(avg_scores),
            "source": "mood_table",
        }

    def _build_focus(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Build focus & screen time section."""
        focus_rows = _query_focus_range(
            conn, self.start_date.isoformat(), self.end_date.isoformat(),
        )
        screen_rows = _query_metric_range(
            conn, PHONE_METRICS["screen_time_minutes"],
            self.start_date.isoformat(), self.end_date.isoformat(),
        )

        result: dict[str, Any] = {"available": False}

        if focus_rows:
            working_rows = [r for r in focus_rows if r["state"] == "working"]
            if working_rows:
                total_work_secs = sum(r["total_seconds"] for r in working_rows)
                total_work_hours = round(total_work_secs / 3600, 1)
                result["focus"] = {
                    "total_hours": total_work_hours,
                    "total_sessions": sum(r["session_count"] for r in working_rows),
                    "days_with_focus": len({r["date"] for r in working_rows}),
                }
                result["available"] = True

        if screen_rows:
            screen_values = [r["value"] for r in screen_rows]
            result["screen_time"] = {
                "average_daily_minutes": _avg(screen_values),
                "total_minutes": int(sum(screen_values)),
                "days_tracked": len(screen_values),
            }
            result["available"] = True

        if not result["available"]:
            result["message"] = "No data available"

        return result

    def _build_music(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Build top music section from context/spotify data."""
        spotify_rows = _query_spotify_range(
            conn, self.start_date.isoformat(), self.end_date.isoformat(),
        )
        if not spotify_rows:
            return {"available": False, "message": "No data available"}

        # Parse spotify entries for top tracks/artists
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

        top_artists = sorted(artists.items(), key=lambda x: x[1], reverse=True)[:5]
        top_tracks = sorted(tracks.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "available": True,
            "total_minutes": round(total_minutes, 1) if total_minutes else None,
            "top_artists": [{"name": a[0], "plays": a[1]} for a in top_artists],
            "top_tracks": [{"name": t[0], "plays": t[1]} for t in top_tracks],
            "listening_days": len({row["ts"][:10] for row in spotify_rows}),
        }

    def _build_calendar(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Build calendar events section."""
        events = _query_calendar_events(
            conn, self.start_date.isoformat(), self.end_date.isoformat(),
        )
        if not events:
            return {"available": False, "message": "No data available"}

        return {
            "available": True,
            "total_events": len(events),
            "events": [
                {
                    "title": e.get("title", "Unknown"),
                    "start": e.get("start_time", ""),
                    "end": e.get("end_time", ""),
                    "source": e.get("source", "unknown"),
                }
                for e in events[:20]  # Limit to 20 events
            ],
        }

    def _build_renewals(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Build upcoming renewals section."""
        subs = _query_subscriptions(conn)
        if not subs:
            return {"available": False, "message": "No data available"}

        # Filter to subscriptions with upcoming renewals in the next 30 days
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cutoff = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")

        upcoming = [
            s for s in subs
            if s.get("next_renewal") and s["next_renewal"] <= cutoff
        ]
        # Also include all active subs for the monthly cost view
        monthly_total = sum(
            s["amount"] for s in subs
            if s.get("cycle") == "monthly" and s.get("amount")
        )
        yearly_total = sum(
            s["amount"] for s in subs
            if s.get("cycle") == "yearly" and s.get("amount")
        )

        return {
            "available": True,
            "upcoming": [
                {
                    "service": s["service"],
                    "amount": s["amount"],
                    "currency": s.get("currency", "CAD"),
                    "cycle": s.get("cycle", "monthly"),
                    "next_renewal": s.get("next_renewal", "unknown"),
                    "category": s.get("category", "other"),
                }
                for s in upcoming[:10]
            ],
            "monthly_cost_total": round(monthly_total, 2),
            "yearly_cost_total": round(yearly_total, 2),
            "total_active": len(subs),
        }

    # ── Build report dict ──────────────────────────────────────────────

    def build(self) -> dict[str, Any]:
        """Build the full weekly report dict from database queries."""
        conn = self._get_conn()
        try:
            sleep = self._build_sleep_summary(conn)
            activity = self._build_steps_activity(conn)
            mood = self._build_mood(conn)
            focus = self._build_focus(conn)
            music = self._build_music(conn)
            calendar = self._build_calendar(conn)
            renewals = self._build_renewals(conn)
        finally:
            conn.close()

        start_str = self.start_date.isoformat()
        end_str = self.end_date.isoformat()
        week_num = self.start_date.isocalendar()[1]
        label = f"{self.end_date.year}-W{week_num:02d}"

        # Build narrative summary
        summary_parts = []
        if sleep.get("available"):
            avg = sleep.get("average_hours")
            if avg is not None:
                summary_parts.append(f"avg {avg:.1f}h sleep")
        if activity.get("available") and "steps" in activity:
            summary_parts.append(f"{activity['steps']['total']:,} total steps")
        if mood.get("available"):
            avg = mood.get("average")
            if avg is not None:
                summary_parts.append(f"mood {avg:.1f}/10")
        if focus.get("available") and "focus" in focus:
            summary_parts.append(f"{focus['focus']['total_hours']:.1f}h focus time")

        summary = "; ".join(summary_parts) + "." if summary_parts else "No data available for this week."

        # Build sections list
        sections = [
            {"key": "sleep", "label": "Sleep Summary", "data": sleep},
            {"key": "activity", "label": "Steps & Activity", "data": activity},
            {"key": "mood", "label": "Mood", "data": mood},
            {"key": "focus", "label": "Focus & Screen Time", "data": focus},
            {"key": "music", "label": "Top Music", "data": music},
            {"key": "calendar", "label": "Calendar Events", "data": calendar},
            {"key": "renewals", "label": "Upcoming Renewals", "data": renewals},
        ]

        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_type": "weekly",
            "period_start": start_str,
            "period_end": end_str,
            "week_label": label,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "confidence": self._determine_confidence(sleep, activity, mood, focus),
            "summary": summary,
            "sections": sections,
            "privacy_level": "safe_for_user_dm",
            "encrypted_state": False,
        }

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
        """Render the weekly report as Obsidian-compatible Markdown."""
        if data is None:
            data = self.build()
        lines: list[str] = []

        label = data.get("week_label", "Weekly")
        start = data.get("period_start", "")
        end = data.get("period_end", "")
        lines.append(f"# 📊 Weekly Report — {label}")
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
            elif key == "calendar":
                self._render_calendar_md(lines, section_data)
            elif key == "renewals":
                self._render_renewals_md(lines, section_data)
            else:
                # Generic fallback
                for k, v in section_data.items():
                    if k not in ("available", "message"):
                        lines.append(f"- **{k}**: {v}")
                lines.append("")

        lines.append("---")
        lines.append(f"*Helios v6 — Weekly Report • {data.get('generated_at', 'unknown')}*")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _render_sleep_md(lines: list[str], data: dict) -> None:
        avg = data.get("average_hours")
        if avg is not None:
            trend = data.get("trend", "stable")
            trend_icon = {"up": "📈", "down": "📉", "stable": "➡️"}.get(trend, "➡️")
            lines.append(f"- **Average sleep**: {avg:.1f}h {trend_icon} {trend}")
            lines.append(f"- **Days tracked**: {data.get('total_days', 0)}")
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
        lines.append("")

    @staticmethod
    def _render_activity_md(lines: list[str], data: dict) -> None:
        steps = data.get("steps")
        if steps:
            lines.append(f"- **Total steps**: {steps.get('total', 0):,}")
            lines.append(f"- **Average daily**: {steps.get('average', 0):,.0f}")
            lines.append(f"- **Days tracked**: {steps.get('days_tracked', 0)}")
        active = data.get("active_minutes")
        if active:
            lines.append(f"- **Active minutes**: {active.get('total', 0):,} total ({active.get('average', 0):.0f} avg/day)")
        lines.append("")

    @staticmethod
    def _render_mood_md(lines: list[str], data: dict) -> None:
        avg = data.get("average")
        if avg is not None:
            trend = data.get("trend", "stable")
            trend_icon = {"up": "📈", "down": "📉", "stable": "➡️"}.get(trend, "➡️")
            lines.append(f"- **Average mood**: {avg:.1f}/10 {trend_icon} {trend}")
            lines.append(f"- **Days tracked**: {data.get('days_tracked', 0)}")
        lines.append("")

    @staticmethod
    def _render_focus_md(lines: list[str], data: dict) -> None:
        focus = data.get("focus")
        if focus:
            lines.append(f"- **Total focus time**: {focus.get('total_hours', 0):.1f}h")
            lines.append(f"- **Focus sessions**: {focus.get('total_sessions', 0)}")
            lines.append(f"- **Days with focus**: {focus.get('days_with_focus', 0)}")
        screen = data.get("screen_time")
        if screen:
            lines.append(f"- **Screen time avg**: {screen.get('average_daily_minutes', 0):.0f} min/day")
            lines.append(f"- **Total screen time**: {screen.get('total_minutes', 0):,} min")
        lines.append("")

    @staticmethod
    def _render_music_md(lines: list[str], data: dict) -> None:
        if data.get("total_minutes"):
            lines.append(f"- **Total listening**: {data['total_minutes']:.0f} min")
        if data.get("listening_days"):
            lines.append(f"- **Listening days**: {data['listening_days']}")
        artists = data.get("top_artists", [])
        if artists:
            lines.append("- **Top artists**:")
            for a in artists:
                lines.append(f"  1. {a['name']} ({a['plays']} plays)")
        tracks = data.get("top_tracks", [])
        if tracks:
            lines.append("- **Top tracks**:")
            for t in tracks[:3]:
                lines.append(f"  1. {t['name']} ({t['plays']} plays)")
        lines.append("")

    @staticmethod
    def _render_calendar_md(lines: list[str], data: dict) -> None:
        lines.append(f"- **Total events**: {data.get('total_events', 0)}")
        for event in data.get("events", [])[:10]:
            start = event.get("start", "?")[:10]
            lines.append(f"  - {event.get('title', '?')} ({start})")
        lines.append("")

    @staticmethod
    def _render_renewals_md(lines: list[str], data: dict) -> None:
        lines.append(f"- **Monthly cost**: ${data.get('monthly_cost_total', 0):.2f} CAD")
        lines.append(f"- **Yearly cost**: ${data.get('yearly_cost_total', 0):.2f} CAD")
        lines.append(f"- **Active subscriptions**: {data.get('total_active', 0)}")
        upcoming = data.get("upcoming", [])
        if upcoming:
            lines.append("- **Upcoming renewals**:")
            for sub in upcoming:
                lines.append(
                    f"  - {sub['service']}: ${sub['amount']:.2f} ({sub['cycle']}) — {sub.get('next_renewal', '?')}"
                )
        lines.append("")

    # ── File writing ────────────────────────────────────────────────────

    def generate(self) -> tuple[str, Optional[Path]]:
        """Generate the weekly report: return markdown string and write to Obsidian.

        Returns
        -------
        (markdown, obsidian_path)
            markdown: The rendered Markdown string.
            obsidian_path: Path to the written Obsidian file, or None
                           if vault is not configured.
        """
        data = self.build()
        md = self.render_markdown(data)

        # Also write via report helpers (JSON + Markdown)
        from datetime import date as dt
        week_num = self.start_date.isocalendar()[1]
        date_str = f"{self.end_date.year}-W{week_num:02d}"

        try:
            write_report_json(data, "weekly", date_str)
            write_report_markdown(data, "weekly", date_str)
        except Exception as exc:
            logger.warning("Failed to write report files: %s", exc)

        # Write to Obsidian vault
        obsidian_path: Optional[Path] = None
        if self.vault_path:
            try:
                weekly_dir = self.vault_path / "Helios" / "Weekly"
                weekly_dir.mkdir(parents=True, exist_ok=True)
                filename = f"{self.end_date.year}-W{week_num:02d}.md"
                obsidian_path = weekly_dir / filename
                obsidian_path.write_text(md, encoding="utf-8")
                logger.info("Weekly report written to Obsidian: %s", obsidian_path)
            except Exception as exc:
                logger.error("Failed to write weekly report to Obsidian: %s", exc)

        return md, obsidian_path


# ── Standalone Markdown rendering (for report.v1 dispatch) ─────────────

def render_weekly_markdown(data: dict[str, Any]) -> str:
    """Render a weekly report dict to Markdown (for report.v1 dispatch)."""
    report = WeeklyReport.__new__(WeeklyReport)
    # We only need render_markdown; skip __init__
    return WeeklyReport.render_markdown(report, data)