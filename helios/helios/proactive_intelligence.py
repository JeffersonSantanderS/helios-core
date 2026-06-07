"""
Helios v6 — Proactive Intelligence Engine (Phase 3).

Watches Helios data for actionable patterns,
generates predictive recommendations, trend alerts, and weekly digests.

Architecture:
    - DailyHealthScore: computes wellness composite from sleep/activity/mood/HR
    - TrendDetector: flags concerning multi-day streaks
    - Recommender: generates personalized, correlation-backed suggestions
    - WeeklyDigest: summarizes week's patterns and insights

Integration:
    - Lightweight checks run every tick (trend alerts, rapid anomalies)
    - Full analysis runs inside dream cycle (recommendations, weekly digest)
    - Results pushed to Discord when actionable
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("helios.proactive")

# ── Metric display names ────────────────────────────────────────────────────
METRIC_LABELS = {
    "sleep.hours": "Sleep",
    "activity.stand_minutes": "Activity",
    "mood.score_daily": "Mood",
    "resting_heart_rate.avg_daily": "Resting HR",
    "spotify.listen_minutes_daily": "Spotify",
    "spotify.tracks_daily": "Tracks",
    "protein.grams_daily": "Protein",
    "weather.temp_max": "High Temp",
}

# ── Score thresholds ────────────────────────────────────────────────────────
SLEEP_IDEAL = 8.0
SLEEP_MIN = 6.0
SLEEP_DANGER = 4.0
ACTIVITY_IDEAL = 60
ACTIVITY_MIN = 20
MOOD_IDEAL = 8
MOOD_MIN = 4
HEARTRATE_IDEAL_LOW = 55
HEARTRATE_IDEAL_HIGH = 70
HEARTRATE_DANGER = 85


class DailyHealthScore:
    """Computes a 0-100 daily wellness composite.

    Missing metrics are marked as 'missing', not scored as 0.
    The total is normalized to available components so a partial
    score doesn't appear artificially poor.
    """

    # Component weights
    # Component weights — use stand_minutes (actual movement time) not exercise_minutes (Apple Watch's stricter definition)
    COMPONENTS = {
        "sleep": {"key": "sleep.hours", "max": 35},
        "activity": {"key": "activity.stand_minutes", "max": 25},
        "mood": {"key": "mood.score_daily", "max": 25},
        "hr": {"key": "resting_heart_rate.avg_daily", "max": 15},
    }

    @staticmethod
    def compute(metrics: dict[str, float]) -> dict:
        sleep = metrics.get("sleep.hours")
        # Prefer stand_minutes (actual movement); fall back to exercise_minutes then minutes_daily for historical compat
        activity = metrics.get("activity.stand_minutes") or metrics.get("activity.exercise_minutes") or metrics.get("activity.minutes_daily")
        mood = metrics.get("mood.score_daily")
        hr = metrics.get("resting_heart_rate.avg_daily")

        scores: dict[str, dict] = {}
        missing_metrics: list[str] = []

        # Sleep (0-35 points)
        if sleep is None:
            scores["sleep"] = {"score": None, "max": 35, "status": "missing"}
            missing_metrics.append("sleep.hours")
        elif sleep <= 0:
            scores["sleep"] = {"score": 0, "max": 35, "status": "present"}
        elif sleep >= SLEEP_IDEAL:
            scores["sleep"] = {"score": 35, "max": 35, "status": "present"}
        elif sleep < SLEEP_DANGER:
            scores["sleep"] = {"score": max(0, int(sleep / SLEEP_DANGER * 10)), "max": 35, "status": "present"}
        elif sleep < SLEEP_MIN:
            scores["sleep"] = {"score": int(10 + (sleep - SLEEP_DANGER) / (SLEEP_MIN - SLEEP_DANGER) * 15), "max": 35, "status": "present"}
        else:
            scores["sleep"] = {"score": int(25 + (sleep - SLEEP_MIN) / (SLEEP_IDEAL - SLEEP_MIN) * 10), "max": 35, "status": "present"}

        # Activity (0-25 points)
        if activity is None:
            scores["activity"] = {"score": None, "max": 25, "status": "missing"}
            missing_metrics.append("activity.stand_minutes")
        elif activity <= 0:
            scores["activity"] = {"score": 0, "max": 25, "status": "present"}
        elif activity >= ACTIVITY_IDEAL:
            scores["activity"] = {"score": 25, "max": 25, "status": "present"}
        elif activity < ACTIVITY_MIN:
            scores["activity"] = {"score": max(0, int(activity / ACTIVITY_MIN * 5)), "max": 25, "status": "present"}
        else:
            scores["activity"] = {"score": int(5 + (activity - ACTIVITY_MIN) / (ACTIVITY_IDEAL - ACTIVITY_MIN) * 20), "max": 25, "status": "present"}

        # Mood (0-25 points) — only score if data exists
        if mood is None:
            scores["mood"] = {"score": None, "max": 25, "status": "missing"}
            missing_metrics.append("mood.score_daily")
        elif mood <= 0:
            scores["mood"] = {"score": 0, "max": 25, "status": "present"}
        elif mood >= MOOD_IDEAL:
            scores["mood"] = {"score": 25, "max": 25, "status": "present"}
        elif mood < MOOD_MIN:
            scores["mood"] = {"score": max(0, int(mood / MOOD_MIN * 5)), "max": 25, "status": "present"}
        else:
            scores["mood"] = {"score": int(5 + (mood - MOOD_MIN) / (MOOD_IDEAL - MOOD_MIN) * 20), "max": 25, "status": "present"}

        # Resting HR (0-15 points) — lower is generally better within reason
        if hr is None:
            scores["hr"] = {"score": None, "max": 15, "status": "missing"}
            missing_metrics.append("resting_heart_rate.avg_daily")
        elif hr <= 0:
            scores["hr"] = {"score": 0, "max": 15, "status": "present"}
        elif hr >= HEARTRATE_DANGER:
            scores["hr"] = {"score": 2, "max": 15, "status": "present"}
        elif HEARTRATE_IDEAL_LOW <= hr <= HEARTRATE_IDEAL_HIGH:
            scores["hr"] = {"score": 15, "max": 15, "status": "present"}
        elif hr < HEARTRATE_IDEAL_LOW:
            scores["hr"] = {"score": 10, "max": 15, "status": "present"}  # very low HR, possibly athlete or recovery
        else:
            scores["hr"] = {"score": int(15 - (hr - HEARTRATE_IDEAL_HIGH) / (HEARTRATE_DANGER - HEARTRATE_IDEAL_HIGH) * 13), "max": 15, "status": "present"}

        # Compute totals
        total = sum(c["score"] for c in scores.values() if c["score"] is not None)
        max_available = sum(c["max"] for c in scores.values() if c["score"] is not None)
        total_max = sum(c["max"] for c in scores.values())
        is_partial = len(missing_metrics) > 0

        # Normalize to 100-point scale for consistency
        normalized = int((total / max_available) * 100) if max_available > 0 else 0

        # Grade based on normalized score
        grade = (
            "🟢 Excellent" if normalized >= 80 else
            "🔵 Good" if normalized >= 65 else
            "🟡 Fair" if normalized >= 45 else
            "🟠 Poor" if normalized >= 25 else
            "🔴 Critical"
        )

        # Build human-readable breakdown
        breakdown_parts = []
        for name in ["sleep", "activity", "mood", "hr"]:
            c = scores[name]
            if c["score"] is not None:
                breakdown_parts.append(f"{name.title()}: {c['score']}/{c['max']}")
            else:
                breakdown_parts.append(f"{name.title()}: missing")

        prefix = "Partial" if is_partial else "Daily"
        return {
            "total": normalized,
            "raw_total": total,
            "max_available": max_available,
            "total_max": total_max,
            "normalized_total": normalized,
            "grade": grade,
            "is_partial": is_partial,
            "missing_metrics": missing_metrics,
            "components": {k: {"score": v["score"], "max": v["max"], "status": v["status"]} for k, v in scores.items()},
            "breakdown": " | ".join(breakdown_parts),
        }


class TrendDetector:
    """Detects concerning multi-day streaks and thresholds."""

    @staticmethod
    def detect(db, days: int = 14) -> list[dict]:
        conn = db._conn()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

        alerts = []

        # ── Sleep streak: days below 5h ──
        cur = conn.execute(
            "SELECT date_key, value FROM metric_snapshots "
            "WHERE metric='sleep.hours' AND date_key >= ? "
            "ORDER BY date_key DESC",
            (cutoff,)
        )
        sleep_rows = list(cur.fetchall())
        # Count consecutive days ≤ danger threshold, skipping NULLs
        low_sleep_streak = 0
        for _, val in sleep_rows:
            if val is None:
                continue  # skip missing days, don't break streak
            if val <= SLEEP_DANGER:
                low_sleep_streak += 1
            else:
                break  # streak broken
        if low_sleep_streak >= 3:
            alerts.append({
                "type": "sleep_streak",
                "severity": "warning" if low_sleep_streak < 5 else "critical",
                "category": "health",
                "title": f"{low_sleep_streak} days of low sleep",
                "detail": (
                    f"You've slept under {SLEEP_DANGER:.0f}h for {low_sleep_streak} consecutive days. "
                    f"Low activity often follows poor sleep in your data. "
                    f"One full night of sleep could reset this."
                ),
                "urgent": low_sleep_streak >= 5,
            })

        # ── Activity gap: days without exercise ──
        cur = conn.execute(
            "SELECT date_key, value FROM metric_snapshots "
            "WHERE metric='activity.stand_minutes' AND date_key >= ? "
            "ORDER BY date_key DESC",
            (cutoff,)
        )
        act_rows = list(cur.fetchall())
        low_act_streak = 0
        for _, val in act_rows:
            if val is not None and val < ACTIVITY_MIN:
                low_act_streak += 1
            else:
                break
        if low_act_streak >= 4:
            # Use actual correlation from inferred patterns if available
            corr_str = "Your data suggests activity and sleep are linked"
            try:
                patterns_file = Path.home() / ".hermes" / "helios" / "data" / "inferred_patterns.json"
                if patterns_file.exists():
                    patterns = json.loads(patterns_file.read_text())
                    sleep_corr = patterns.get("low_sleep_correlates") or {}
                    r_val = sleep_corr.get("value", {}).get("r") if isinstance(sleep_corr.get("value"), dict) else sleep_corr.get("value") if isinstance(sleep_corr, dict) else None
                    if r_val and abs(float(r_val)) >= 0.3:
                        corr_str = f"Your data shows a sleep↔activity correlation (r={float(r_val):.2f})"
            except Exception:
                pass
            alerts.append({
                "type": "activity_gap",
                "severity": "warning",
                "category": "health",
                "title": f"{low_act_streak} days with minimal activity",
                "detail": (
                    f"Activity under {ACTIVITY_MIN}min for {low_act_streak} days. "
                    f"{corr_str} — "
                    f"even a 20-minute walk could improve tonight's sleep."
                ),
                "urgent": low_act_streak >= 7,
            })

        # ── Heart rate elevation ──
        cur = conn.execute(
            "SELECT date_key, value FROM metric_snapshots "
            "WHERE metric='resting_heart_rate.avg_daily' AND date_key >= ? "
            "ORDER BY date_key DESC LIMIT 3",
            (cutoff,)
        )
        hr_rows = list(cur.fetchall())
        elevated_count = sum(1 for _, v in hr_rows if v and v > HEARTRATE_DANGER)
        if elevated_count >= 2:
            alerts.append({
                "type": "heartrate_elevation",
                "severity": "warning",
                "category": "health",
                "title": "Elevated resting heart rate",
                "detail": (
                    f"RHR above {HEARTRATE_DANGER} bpm for {elevated_count} of last 3 readings. "
                    f"This can indicate stress, poor recovery, or illness."
                ),
                "urgent": elevated_count >= 3,
            })

        # ── Mood drop ──
        cur = conn.execute(
            "SELECT date_key, value FROM metric_snapshots "
            "WHERE metric='mood.score_daily' AND date_key >= ? "
            "ORDER BY date_key DESC LIMIT 3",
            (cutoff,)
        )
        mood_rows = list(cur.fetchall())
        if mood_rows and all(v and v <= MOOD_MIN for _, v in mood_rows):
            alerts.append({
                "type": "mood_drop",
                "severity": "warning",
                "category": "health",
                "title": "Consistently low mood",
                "detail": (
                    f"Mood at or below {MOOD_MIN}/10 for {len(mood_rows)} consecutive days. "
                    f"Exercise is the strongest mood booster in your data."
                ),
                "urgent": len(mood_rows) >= 4,
            })

        return alerts


class Recommender:
    """Generates personalized, correlation-backed recommendations."""

    @staticmethod
    def recommend(db) -> list[dict]:
        conn = db._conn()
        recommendations = []

        # ── Pull current metrics ──
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cur = conn.execute(
            "SELECT metric, value FROM metric_snapshots WHERE date_key=?",
            (today,)
        )
        today_metrics = {r[0]: r[1] for r in cur.fetchall()}

        sleep = today_metrics.get("sleep.hours")
        activity = today_metrics.get("activity.stand_minutes") or today_metrics.get("activity.exercise_minutes") or today_metrics.get("activity.minutes_daily")
        mood = today_metrics.get("mood.score_daily")
        hr = today_metrics.get("resting_heart_rate.avg_daily")
        spotify = today_metrics.get("spotify.listen_minutes_daily")

        # ── Pull strongest correlations ──
        cur = conn.execute(
            "SELECT metric_a, metric_b, pearson_r, strength, direction, n_observations "
            "FROM correlations WHERE strength='strong' AND p_value < 0.01 "
            "ORDER BY ABS(pearson_r) DESC LIMIT 5"
        )
        corrs = list(cur.fetchall())

        # ── Generate recommendations ──

        # Sleep is king — strongest correlation in the entire dataset
        if sleep is not None and sleep < SLEEP_MIN:
            # Find what sleep correlates with strongly
            sleep_corrs = [c for c in corrs if "sleep" in c[0] or "sleep" in c[1]]
            if sleep_corrs:
                for corr in sleep_corrs:
                    related = corr[1] if "sleep" in corr[0] else corr[0]
                    label = METRIC_LABELS.get(related, related)
                    direction = "improves" if corr[4] == "positive" else "suffers"
                    recommendations.append({
                        "type": "sleep_optimization",
                        "priority": 3 if sleep < SLEEP_DANGER else 2,
                        "title": "Prioritize sleep tonight",
                        "detail": (
                            f"Your sleep ({sleep:.1f}h) is well below ideal ({SLEEP_IDEAL:.0f}h). "
                            f"Your data shows {label} {direction} sleep (r={corr[2]:.2f}, n={corr[5]}). "
                            f"Recommendation: wind down 30 min earlier, no screens after 10 PM."
                        ),
                    })
                    break  # only one sleep rec per cycle

        # Activity gap
        if activity is not None and activity < ACTIVITY_MIN / 2:
            recommendations.append({
                "type": "activity_nudge",
                "priority": 1,
                "title": "Get moving today",
                "detail": (
                    f"Only {activity:.0f} min activity today. "
                    f"Activity and sleep quality tend to move together in your data. "
                    f"Even a 15-minute walk matters."
                ),
            })

        # Late-night Spotify
        if spotify is not None and sleep is not None and spotify > 180 and sleep < SLEEP_MIN:
            recommendations.append({
                "type": "spotify_late",
                "priority": 1,
                "title": "Music may be keeping you up",
                "detail": (
                    f"{spotify:.0f} min of Spotify today + low sleep ({sleep:.1f}h). "
                    f"Consider setting a listening cutoff 1 hour before bed."
                ),
            })

        # Mood + Activity connection
        if mood is not None and activity is not None and mood <= MOOD_MIN and activity < ACTIVITY_MIN:
            recommendations.append({
                "type": "mood_exercise",
                "priority": 2,
                "title": "Exercise for mood boost",
                "detail": (
                    f"Mood is {mood:.0f}/10 today and activity is minimal. "
                    f"Your strongest mood correlation is with activity — "
                    f"a quick workout could lift your evening."
                ),
            })

        return recommendations


class WeeklyDigest:
    """Summarizes the week's patterns into a Discord-friendly digest."""

    @staticmethod
    def generate(db) -> dict:
        conn = db._conn()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

        # ── Weekly averages ──
        cur = conn.execute(
            "SELECT metric, ROUND(AVG(value), 1), COUNT(*), "
            "       ROUND(MIN(value), 1), ROUND(MAX(value), 1) "
            "FROM metric_snapshots WHERE date_key >= ? AND date_key <= ? "
            "GROUP BY metric ORDER BY metric",
            (week_ago, today)
        )
        avgs = {}
        for row in cur.fetchall():
            avgs[row[0]] = {
                "avg": row[1], "days": row[2], "min": row[3], "max": row[4],
                "label": METRIC_LABELS.get(row[0], row[0]),
            }

        # ── Score trend ──
        scores = []
        cur = conn.execute(
            "SELECT date_key FROM metric_snapshots WHERE date_key >= ? "
            "GROUP BY date_key ORDER BY date_key",
            (week_ago,)
        )
        dates = [r[0] for r in cur.fetchall()]

        # ── Top correlation ──
        cur = conn.execute(
            "SELECT metric_a, metric_b, ROUND(pearson_r, 2), strength, n_observations "
            "FROM correlations WHERE strength='strong' AND p_value < 0.01 "
            "ORDER BY ABS(pearson_r) DESC LIMIT 1"
        )
        top_corr = cur.fetchone()

        # ── Build summary ──
        lines = [f"**📊 Week of {week_ago} → {today}**", ""]

        for metric in ["sleep.hours", "activity.minutes_daily", "mood.score_daily",
                         "resting_heart_rate.avg_daily", "spotify.listen_minutes_daily"]:
            data = avgs.get(metric)
            if data and data["days"] >= 3:
                lines.append(
                    f"• **{data['label']}**: avg {data['avg']} "
                    f"(range: {data['min']}–{data['max']}, {data['days']} days)"
                )

        lines.append("")

        if top_corr:
            la = METRIC_LABELS.get(top_corr[0], top_corr[0])
            lb = METRIC_LABELS.get(top_corr[1], top_corr[1])
            lines.append(
                f"🔗 **Top pattern**: {la} ↔ {lb} — "
                f"r={top_corr[2]} ({top_corr[3]}, n={top_corr[4]})"
            )

        return {
            "title": f"Helios Weekly Digest — {week_ago} to {today}",
            "body": "\n".join(lines),
            "metrics": avgs,
            "top_correlation": top_corr,
        }


class ProactiveIntelligence:
    """Phase 3 orchestrator — plugs into dream cycle and tick loop."""

    def __init__(self, db):
        self.db = db
        self._last_tick_alerts: list[dict] = []
        self._last_weekly: Optional[dict] = None
        self._weekly_check_day: Optional[int] = None  # day of month last weekly ran

    def tick_check(self) -> list[dict]:
        """Lightweight check every tick — trend alerts only. Returns actionable alerts."""
        try:
            alerts = TrendDetector.detect(self.db, days=14)
            self._last_tick_alerts = alerts
            return [a for a in alerts if a.get("urgent")]
        except Exception as exc:
            log.warning("Proactive tick check failed: %s", exc)
            return []

    def dream_analysis(self) -> dict:
        """Full analysis during dream cycle — recommendations + weekly."""
        result = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "recommendations": [],
            "weekly_digest": None,
            "health_score": None,
            "alerts": [],
        }

        try:
            # Health score for today
            conn = self.db._conn()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            cur = conn.execute(
                "SELECT metric, value FROM metric_snapshots WHERE date_key=?",
                (today,)
            )
            today_metrics = {r[0]: r[1] for r in cur.fetchall()}
            if today_metrics:
                result["health_score"] = DailyHealthScore.compute(today_metrics)

            # Recommendations
            result["recommendations"] = Recommender.recommend(self.db)

            # Weekly digest (only on Sundays)
            now = datetime.now()
            if now.weekday() == 6 and self._weekly_check_day != now.day:
                self._weekly_check_day = now.day
                result["weekly_digest"] = WeeklyDigest.generate(self.db)
                self._last_weekly = result["weekly_digest"]

        except Exception as exc:
            log.warning("Dream analysis failed: %s", exc, exc_info=True)
            result["error"] = str(exc)

        return result

    # ── Formatting for Discord push ───────────────────────────────────────

    @staticmethod
    def format_recommendations(recs: list[dict]) -> Optional[str]:
        """Format recommendations for Discord push. Returns None if nothing worth pushing."""
        urgent = [r for r in recs if r.get("priority", 0) >= 2]
        if not urgent:
            return None

        lines = ["**🧠 Helios Insights**"]
        for r in urgent[:3]:
            icon = "🚨" if r["priority"] >= 3 else "💡"
            lines.append(f"\n{icon} **{r['title']}**\n_{r['detail']}_")

        return "\n".join(lines)

    @staticmethod
    def format_health_score(score: dict) -> str:
        label = f"**{score['grade']}**"
        if score.get("is_partial"):
            label += " (partial)"
        else:
            label += f" — {score['total']}/100"
        return (
            f"{label}\n"
            f"{score['breakdown']}"
        )

    # ── Status ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "module": "proactive_intelligence",
            "phase": 3,
            "last_weekly_day": self._weekly_check_day,
            "pending_tick_alerts": len(self._last_tick_alerts),
        }
