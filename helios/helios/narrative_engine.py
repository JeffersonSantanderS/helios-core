"""
Helios v6 — Narrative Engine (Phase 4+).

Assembles telemetry into readable prose.
No LLM needed — structured template assembly from real data.

Design principles:
  1. Every sentence backed by a number in the database
  2. Compares to historical baselines, not just reports raw averages
  3. Highlights what changed — up/down/stable with context
  4. Actionable closing — what the data says you should do
"""

from __future__ import annotations

import json, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("helios.narrative")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"

METRIC_LABELS = {
    "sleep.hours": "Sleep", "activity.minutes_daily": "Activity",
    "mood.score_daily": "Mood", "resting_heart_rate.avg_daily": "Resting HR",
    "spotify.listen_minutes_daily": "Spotify", "spotify.tracks_daily": "Tracks",
}

GOOD_SLEEP = 7.0
LOW_SLEEP = 5.0
GOOD_ACTIVITY = 60
LOW_ACTIVITY = 20
GOOD_MOOD = 7
LOW_MOOD = 4
GOOD_HR_HIGH = 70


def _pct_rank(values: list[float], target: float) -> float:
    """What percentile is target within values? 0=lowest, 100=highest."""
    if not values:
        return 50.0
    ranked = sum(1 for v in values if v < target)
    return (ranked / len(values)) * 100


def _describe_metric(value: float, label: str, unit: str,
                     baseline_avg: float, all_vals: list[float],
                     good_threshold: float, low_threshold: float) -> str:
    """Generate natural-language description of a metric vs its history."""
    rank = _pct_rank(all_vals, value)
    delta = value - baseline_avg
    abs_delta = abs(delta)

    if rank >= 80:
        descriptor = f"averaged {value}{unit} — your best week in a month"
    elif rank >= 60:
        descriptor = f"held strong at {value}{unit}, above your usual range"
    elif rank <= 20:
        descriptor = f"dropped to {value}{unit} — lowest in the last {len(all_vals)} weeks"
    elif rank <= 40:
        descriptor = f"came in low at {value}{unit}, below your typical baseline of {baseline_avg:.0f}{unit}"
    elif abs_delta <= 1 and baseline_avg > 0:
        descriptor = f"averaged {value}{unit}, matching your typical baseline"
    elif value <= low_threshold:
        descriptor = f"was only {value}{unit} — well below healthy minimum"
    elif value >= good_threshold:
        descriptor = f"sat comfortably at {value}{unit}"
    else:
        direction = "above" if delta > 0 else "below"
        descriptor = f"averaged {value}{unit}, {abs_delta:.0f}{unit} {direction} your norm of {baseline_avg:.0f}{unit}"

    return descriptor


class NarrativeEngine:
    """Assembles Helios data into readable weekly prose."""

    def __init__(self, db):
        self.db = db

    def generate(self) -> str:
        """Generate the full narrative digest. Returns Discord-formatted markdown string."""
        conn = self.db._conn()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

        # ── 1. Gather data ────────────────────────────────────────────
        metrics = self._get_weekly_metrics(conn, week_ago, today)
        baselines = self._get_baselines(conn, month_ago)
        correlations = self._get_top_correlations(conn)
        predictions = self._get_pending_predictions(conn)

        # ── 2. Build sections ─────────────────────────────────────────

        lines = [f"**📖 Helios — Week of {week_ago} to {today}**", ""]

        # ── Section: Location ─────────────────────────────────────────
        location_section = self._build_location_section(today, month_ago)
        if location_section:
            lines.append(location_section)
            lines.append("")

        # ── Section: Health ───────────────────────────────────────────
        health_lines = []
        for metric_key in ["sleep.hours", "activity.minutes_daily",
                            "mood.score_daily", "resting_heart_rate.avg_daily"]:
            if metric_key not in metrics:
                continue

            val = metrics[metric_key]["avg"]
            label = METRIC_LABELS.get(metric_key, metric_key)
            base = baselines.get(metric_key, {})
            bl_avg = base.get("avg", val)
            bl_all = base.get("all_vals", [])

            if metric_key == "sleep.hours":
                sentence = _describe_metric(val, label, "h", bl_avg, bl_all,
                                            GOOD_SLEEP, LOW_SLEEP)
            elif metric_key == "activity.minutes_daily":
                sentence = _describe_metric(val, label, "min", bl_avg, bl_all,
                                            GOOD_ACTIVITY, LOW_ACTIVITY)
            elif metric_key == "mood.score_daily":
                sentence = _describe_metric(val, label, "/10", bl_avg, bl_all,
                                            GOOD_MOOD, LOW_MOOD)
            elif metric_key == "resting_heart_rate.avg_daily":
                sentence = _describe_metric(val, label, "bpm", bl_avg, bl_all,
                                            GOOD_HR_HIGH, 999)
            else:
                sentence = f"{label} averaged {val}"

            # Add context from correlations if available
            extra = ""
            if metric_key == "sleep.hours" and val < LOW_SLEEP:
                for corr in correlations:
                    if "sleep" in corr["pair"]:
                        partner = corr["pair"].replace("sleep.hours↔", "").replace("↔sleep.hours", "")
                        partner_label = METRIC_LABELS.get(partner, partner)
                        extra = (f"\n   📊 Your strongest correlation held: {partner_label} "
                                 f"and sleep move together (r={corr['r']}). "
                                 f"Every low-sleep night, {partner_label} follows.")
                        break

            health_lines.append(f"• 💤 {sentence}{extra}" if metric_key == "sleep.hours" else
                                f"• 🏃 {sentence}" if "activity" in metric_key else
                                f"• 🙂 {sentence}" if "mood" in metric_key else
                                f"• 💪 held at a healthy {metrics[metric_key]['avg']}bpm")

        lines.append("**Health**")
        lines.extend(health_lines)
        lines.append("")

        # ── Section: Spotify ──────────────────────────────────────────
        spotify_section = self._build_spotify_section(today)
        if spotify_section:
            lines.append("**What you listened to**")
            lines.append(spotify_section)
            lines.append("")

        # ── Section: Trends ───────────────────────────────────────────
        trends = self._build_trend_summary(metrics, baselines)
        if trends:
            lines.append(f"**📈 Trends:** {trends}")
            lines.append("")

        # ── Section: Looking Ahead ────────────────────────────────────
        if predictions:
            lines.append("**🔮 Looking ahead**")
            for p in predictions[:2]:
                lines.append(f"• {p}")
            lines.append("")

        # ── Footer ────────────────────────────────────────────────────
        lines.append("---")
        lines.append("*Helios v6 — All claims backed by your data*")

        return "\n".join(lines)

    # ── Data gathering helpers ──────────────────────────────────────

    def _get_weekly_metrics(self, conn, start: str, end: str) -> dict:
        """Pull weekly averages and ranges per metric."""
        cur = conn.execute(
            "SELECT metric, ROUND(AVG(value), 1), COUNT(*), "
            "       ROUND(MIN(value), 1), ROUND(MAX(value), 1) "
            "FROM metric_snapshots WHERE date_key >= ? AND date_key <= ? "
            "AND metric IN ('sleep.hours','activity.minutes_daily',"
            "'mood.score_daily','resting_heart_rate.avg_daily',"
            "'spotify.listen_minutes_daily','spotify.tracks_daily') "
            "GROUP BY metric",
            (start, end)
        )
        return {
            r[0]: {"avg": r[1], "days": r[2], "min": r[3], "max": r[4]}
            for r in cur.fetchall()
        }

    def _get_baselines(self, conn, cutoff: str) -> dict:
        """Pull per-metric 30-day averages and all individual values."""
        cur = conn.execute(
            "SELECT metric, AVG(value), GROUP_CONCAT(value) "
            "FROM metric_snapshots WHERE date_key >= ? "
            "AND metric IN ('sleep.hours','activity.minutes_daily',"
            "'mood.score_daily','resting_heart_rate.avg_daily') "
            "GROUP BY metric",
            (cutoff,)
        )
        baselines = {}
        for r in cur.fetchall():
            vals = [float(x) for x in (r[2] or "").split(",") if x.strip()]
            baselines[r[0]] = {
                "avg": round(r[1], 1),
                "all_vals": vals,
            }
        return baselines

    def _get_top_correlations(self, conn) -> list[dict]:
        cur = conn.execute(
            "SELECT metric_a, metric_b, ROUND(pearson_r, 2) "
            "FROM correlations WHERE strength='strong' AND p_value < 0.01 "
            "ORDER BY ABS(pearson_r) DESC LIMIT 3"
        )
        return [{"pair": f"{r[0]}↔{r[1]}", "r": r[2]} for r in cur.fetchall()]

    def _get_pending_predictions(self, conn) -> list[str]:
        """Get latest pending predictions, deduplicated by metric."""
        cur = conn.execute(
            "SELECT metric, predicted_value, days_ahead "
            "FROM prediction_outcomes WHERE resolved=0 "
            "ORDER BY prediction_ts DESC"
        )
        items = []
        seen: set[str] = set()
        for r in cur.fetchall():
            metric = r[0]
            if metric in seen:
                continue
            seen.add(metric)
            label = METRIC_LABELS.get(metric, metric)
            days = r[2]
            val = r[1]
            target_date = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%A")
            items.append(f"**{label}** projected at {val:.1f}h by {target_date}"
                         if "sleep" in metric else
                         f"**{label}** projected at {val:.0f} by {target_date}")
            if len(items) >= 2:
                break
        return items

    # ── Section builders ─────────────────────────────────────────────

    def _build_location_section(self, today: str, month_ago: str) -> str | None:
        """Build location narrative from history JSONL."""
        loc_file = DATA_DIR / "location_history.jsonl"
        if not loc_file.exists():
            return None

        # Parse last 7 days
        week_data = []
        with open(loc_file) as f:
            for line in f:
                try:
                    d = json.loads(line.strip())
                    if d.get("ts", "")[:10] >= (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d"):
                        week_data.append(d)
                except Exception:
                    continue

        if not week_data:
            return None

        # Find transitions: home departures and arrivals per day
        from collections import defaultdict
        by_date = defaultdict(list)
        for d in week_data:
            dt = datetime.fromisoformat(d["ts"].replace("Z", "+00:00"))
            mdt = dt - timedelta(hours=6)
            by_date[mdt.strftime("%a %b %d")].append({
                "time": mdt,
                "lat": d.get("lat"),
                "lon": d.get("lon"),
                "city": d.get("city", "Unknown"),
                "province": d.get("province", ""),
            })

        # Detect home vs away patterns
        # Home: ~51.165, -113.961; Job site south: ~50.777, -114.144; SE: ~51.010, -114.004
        def is_home(lat, lon):
            return abs(lat - 51.165) < 0.01 and abs(lon - (-113.961)) < 0.02

        def classify(lat, lon):
            if is_home(lat, lon):
                return "home"
            if abs(lat - 50.777) < 0.01 and abs(lon - (-114.144)) < 0.03:
                return "south job site"
            if abs(lat - 51.010) < 0.01 and abs(lon - (-114.004)) < 0.01:
                return "SE"
            return "elsewhere"

        days_with_movement = 0
        job_visits: set[str] = set()
        departures = []
        arrivals_home = []

        for date_label, points in sorted(by_date.items()):
            if len(points) < 3:
                continue

            # Find first non-home and last non-home
            left_home = None
            returned_home = None
            prev_loc = classify(points[0]["lat"], points[0]["lon"])
            day_locations: set[str] = set()

            for p in points[1:]:
                loc = classify(p["lat"], p["lon"])
                if prev_loc == "home" and loc != "home":
                    left_home = p["time"]
                if prev_loc != "home" and loc == "home":
                    returned_home = p["time"]
                if loc not in ("home", "elsewhere"):
                    day_locations.add(loc)
                prev_loc = loc

            if left_home:
                departures.append(left_home.hour * 60 + left_home.minute)
            if returned_home:
                arrivals_home.append(returned_home.hour * 60 + returned_home.minute)
            if day_locations:
                days_with_movement += 1
                job_visits |= day_locations  # union — each site appears once

        if days_with_movement == 0:
            return "🏠 You were home all week."

        parts = []

        # Where did you go?
        location_parts = []
        for loc in sorted(job_visits):
            location_parts.append(loc)
        if location_parts:
            parts.append(f"🏗️ You worked at {' and at '.join(location_parts)} across {days_with_movement} days")

        # When did you leave?
        if departures:
            avg_depart = sum(departures) / len(departures)
            dep_h = int(avg_depart // 60)
            dep_m = int(avg_depart % 60)
            parts.append(f"typically left home around {dep_h}:{dep_m:02d} AM")

        # When did you get back?
        if arrivals_home:
            avg_arrive = sum(arrivals_home) / len(arrivals_home)
            arr_h = int(avg_arrive // 60)
            arr_m = int(avg_arrive % 60)
            pm = "PM" if arr_h >= 12 else "AM"
            arr_12h = arr_h if arr_h <= 12 else arr_h - 12
            parts.append(f"home by {arr_12h}:{arr_m:02d} {pm}")

        return " · ".join(parts) + "."

    def _build_spotify_section(self, today: str) -> str | None:
        """Extract top artists and tracks from the week's Spotify history."""
        hist_file = DATA_DIR / "spotify_history.jsonl"
        if not hist_file.exists():
            return None

        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        artist_count: dict[str, int] = defaultdict(int)
        track_count: dict[str, str] = {}  # track_id -> "Track by Artist"

        with open(hist_file) as f:
            for line in f:
                try:
                    d = json.loads(line.strip())
                    if d.get("ts", "")[:10] < cutoff:
                        continue
                    if not d.get("track_id"):
                        continue
                    for artist in d.get("artists", []):
                        artist_count[artist] += 1
                    track_count[d["track_id"]] = f"{d.get('track','?')} by {', '.join(d.get('artists',['?']))}"
                except Exception:
                    continue

        if not artist_count:
            return None

        top_artists = sorted(artist_count.items(), key=lambda x: -x[1])[:3]
        artist_str = ", ".join(f"**{a}**" for a, _ in top_artists)

        # Find most played track (by poll count)
        track_polls: dict[str, int] = defaultdict(int)
        with open(hist_file) as f:
            for line in f:
                try:
                    d = json.loads(line.strip())
                    if d.get("ts", "")[:10] < cutoff:
                        continue
                    if d.get("track_id"):
                        track_polls[d["track_id"]] += 1
                except Exception:
                    continue

        top_track_id = max(track_polls, key=track_polls.get) if track_polls else None
        if top_track_id and top_track_id in track_count:
            return f"You spent the most time with {artist_str}. **{track_count[top_track_id]}** was the track of the week."

        return f"You spent the most time with {artist_str}."

    def _build_trend_summary(self, metrics: dict, baselines: dict) -> str:
        """One-line trend summary by comparing this week to 4-week baseline."""
        trends = []
        for metric_key in ["sleep.hours", "activity.minutes_daily",
                            "mood.score_daily", "resting_heart_rate.avg_daily"]:
            if metric_key not in metrics:
                continue
            val = metrics[metric_key]["avg"]
            base = baselines.get(metric_key, {}).get("avg", val)
            if base and base > 0:
                delta_pct = ((val - base) / base) * 100
                label = METRIC_LABELS.get(metric_key, metric_key)
                if delta_pct >= 15:
                    trends.append(f"{label} ↑")
                elif delta_pct <= -15:
                    trends.append(f"{label} ↓")
                else:
                    trends.append(f"{label} →")

        return " | ".join(trends) if trends else ""


# Need defaultdict for spotify section
from collections import defaultdict
