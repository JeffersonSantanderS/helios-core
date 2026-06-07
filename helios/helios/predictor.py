"""Helios v6 — Predictor.

Runs linear regression over historical metric trends,
projects forward, and warns when dangerous thresholds are approaching.

Design principles:
  1. Cheap — runs every tick, queries <50 rows from SQLite
  2. Honest — reports confidence intervals, not just point estimates
  3. Actionable — only pushes to Discord when a threshold is breached
  4. Quiet — no alerts for healthy data, no spam
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("helios.predictor")

# ── Thresholds (consistent with proactive_intelligence.py) ─────────────────
SLEEP_DANGER = 4.0
SLEEP_MIN = 6.0
ACTIVITY_MIN = 20
MOOD_MIN = 4
HEARTRATE_DANGER = 85

# ── Prediction windows ─────────────────────────────────────────────────────
PREDICT_DAYS = [3, 7]  # project 3 and 7 days ahead
BASELINE_DAYS = 14      # look back 14 days for regression


def _linear_regression(values: list[tuple[int, float]]) -> tuple[float, float, float] | None:
    """Simple linear regression. Returns (slope, intercept, r_squared) or None.
    
    values: list of (day_index, value) where day_index is 0=oldest, N=today.
    """
    n = len(values)
    if n < 5:
        return None
    
    xs = [v[0] for v in values]
    ys = [v[1] for v in values]
    
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denom = sum((x - mean_x) ** 2 for x in xs)
    
    if denom == 0:
        return None
    
    slope = num / denom
    intercept = mean_y - slope * mean_x
    
    # R²
    y_pred = [slope * x + intercept for x in xs]
    ss_res = sum((ys[i] - y_pred[i]) ** 2 for i in range(n))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
    
    return slope, intercept, r_squared


def _std_error(values: list[tuple[int, float]], slope: float, intercept: float) -> float:
    """Standard error of the regression."""
    n = len(values)
    if n <= 2:
        return 0.0
    residuals = [y - (slope * x + intercept) for x, y in values]
    variance = sum(r ** 2 for r in residuals) / (n - 2)
    return variance ** 0.5


def _project(slope: float, intercept: float, today_index: int, days_ahead: int) -> float:
    """Project a value N days into the future."""
    return slope * (today_index + days_ahead) + intercept


# ── Metric-specific predictors ─────────────────────────────────────────────

class SleepPredictor:
    """Predicts sleep trends and flags approaching danger zones."""

    @staticmethod
    def predict(db) -> list[dict]:
        """Return 0-2 alerts if sleep is trending into danger."""
        conn = db._conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=BASELINE_DAYS)).strftime("%Y-%m-%d")
        
        cur = conn.execute(
            "SELECT date_key, value FROM metric_snapshots "
            "WHERE metric='sleep.hours' AND date_key >= ? "
            "ORDER BY date_key ASC",
            (cutoff,)
        )
        rows = list(cur.fetchall())
        
        if len(rows) < 5:
            return []
        
        # Convert to day-indexed values
        base_date = datetime.strptime(rows[0][0], "%Y-%m-%d")
        values = [(int((datetime.strptime(r[0], "%Y-%m-%d") - base_date).days), r[1])
                   for r in rows if r[1] is not None]
        
        if len(values) < 5:
            return []
        
        reg = _linear_regression(values)
        if reg is None:
            return []
        
        slope, intercept, r2 = reg
        today_index = values[-1][0]
        today_value = values[-1][1]
        se = _std_error(values, slope, intercept)
        
        alerts = []
        
        for days_ahead in PREDICT_DAYS:
            projected = _project(slope, intercept, today_index, days_ahead)
            low_bound = projected - (se * 1.5)  # pessimistic: 1.5σ below projection
            
            if projected < SLEEP_MIN and low_bound < SLEEP_DANGER:
                # Danger warning
                target_date = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime("%A %b %d")
                alerts.append({
                    "type": "sleep_projection_danger",
                    "priority": 3,
                    "category": "predictive",
                    "title": f"Sleep trending toward danger zone by {target_date}",
                    "detail": (
                        f"Projected: {projected:.1f}h (pessimistic: {low_bound:.1f}h) in {days_ahead} days. "
                        f"Trend: {slope:+.2f}h/day (r²={r2:.2f}). "
                        f"Current: {today_value:.1f}h. "
                        f"One solid night of 7-8h sleep could reverse this."
                    ),
                    "projected": projected,
                    "low_bound": low_bound,
                    "trend_slope": round(slope, 3),
                    "days_ahead": days_ahead,
                    "days_data": len(values),
                })
                break  # only alert on the nearest danger zone
        
        if not alerts and slope < -0.05 and r2 > 0.3:
            # Declining but not at danger yet — informational
            alerts.append({
                "type": "sleep_projection_decline",
                "priority": 1,
                "category": "predictive",
                "title": "Sleep slowly declining",
                "detail": (
                    f"Sleep trending down {slope:+.2f}h/day (r²={r2:.2f}). "
                    f"Current: {today_value:.1f}h. Still above danger but worth watching."
                ),
            })
        
        return alerts


class ActivityPredictor:
    """Predicts activity trends."""

    @staticmethod
    def predict(db) -> list[dict]:
        conn = db._conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=BASELINE_DAYS)).strftime("%Y-%m-%d")
        
        cur = conn.execute(
            "SELECT date_key, value FROM metric_snapshots "
            "WHERE metric='activity.minutes_daily' AND date_key >= ? "
            "ORDER BY date_key ASC",
            (cutoff,)
        )
        rows = list(cur.fetchall())
        
        if len(rows) < 5:
            return []
        
        base_date = datetime.strptime(rows[0][0], "%Y-%m-%d")
        values = [(int((datetime.strptime(r[0], "%Y-%m-%d") - base_date).days), r[1])
                   for r in rows if r[1] is not None]
        
        reg = _linear_regression(values)
        if reg is None:
            return []
        
        slope, intercept, r2 = reg
        today_value = values[-1][1]
        se = _std_error(values, slope, intercept)
        today_index = values[-1][0]
        
        alerts = []
        
        projected_7d = _project(slope, intercept, today_index, 7)
        if projected_7d < ACTIVITY_MIN and slope < 0:
            target_date = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%A %b %d")
            alerts.append({
                "type": "activity_projection_low",
                "priority": 2,
                "category": "predictive",
                "title": f"Activity may drop below {ACTIVITY_MIN}min/day by {target_date}",
                "detail": (
                    f"Projected: {projected_7d:.0f}min/day in 7 days (trend: {slope:+.1f}min/day). "
                    f"Your sleep quality is strongly linked to activity (r=0.88). "
                    f"Even a 15-minute walk today breaks the trend."
                ),
            })
        
        return alerts


class HealthScorePredictor:
    """Projects the composite health score forward."""

    @staticmethod
    def predict(db) -> list[dict]:
        conn = db._conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=BASELINE_DAYS)).strftime("%Y-%m-%d")
        
        cur = conn.execute(
            "SELECT date_key, metric, value FROM metric_snapshots "
            "WHERE date_key >= ? AND metric IN ('sleep.hours','activity.minutes_daily',"
            "'mood.score_daily','resting_heart_rate.avg_daily') "
            "ORDER BY date_key ASC",
            (cutoff,)
        )
        rows = list(cur.fetchall())
        
        if len(rows) < 20:  # need ~5 days × 4 metrics
            return []
        
        # Compute daily health scores using the same formula as proactive_intelligence.py
        from .proactive_intelligence import DailyHealthScore
        
        # Group by date and compute score for each day
        by_date: dict[str, dict[str, float]] = {}
        for date_key, metric, value in rows:
            if value is None:
                continue
            if date_key not in by_date:
                by_date[date_key] = {}
            flat_metric = (
                "sleep.hours" if metric == "sleep.hours" else
                "activity.minutes_daily" if metric == "activity.minutes_daily" else
                "mood.score_daily" if metric == "mood.score_daily" else
                "resting_heart_rate.avg_daily" if metric == "resting_heart_rate.avg_daily" else
                metric
            )
            by_date[date_key][flat_metric] = value
        
        date_scores: list[tuple[str, float]] = []
        for date_key in sorted(by_date.keys()):
            metrics = by_date[date_key]
            score = DailyHealthScore.compute(metrics)
            date_scores.append((date_key, score["total"]))
        
        if len(date_scores) < 5:
            return []
        
        base_date = datetime.strptime(date_scores[0][0], "%Y-%m-%d")
        values = [(int((datetime.strptime(d, "%Y-%m-%d") - base_date).days), s)
                   for d, s in date_scores]
        
        reg = _linear_regression(values)
        if reg is None:
            return []
        
        slope, intercept, r2 = reg
        current = values[-1][1]
        today_index = values[-1][0]
        se = _std_error(values, slope, intercept)
        
        alerts = []
        
        projected_7d = _project(slope, intercept, today_index, 7)
        pessimistic = projected_7d - (se * 1.5)
        
        if pessimistic < 45 and slope < -1:  # dropping from "Fair" toward "Poor"
            target_date = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%A %b %d")
            alerts.append({
                "type": "health_score_projection",
                "priority": 3 if pessimistic < 25 else 2,
                "category": "predictive",
                "title": f"Overall health trending downward — {pessimistic:.0f}/100 by {target_date}",
                "detail": (
                    f"Health score declining {slope:+.1f}/day (r²={r2:.2f}). "
                    f"Current: {current:.0f}/100. Pessimistic projection: {pessimistic:.0f}/100. "
                    f"This is driven by your sleep/activity/mood trends. "
                    f"A single strong day can dramatically reverse this."
                ),
            })
        
        return alerts


# ── Orchestrator ───────────────────────────────────────────────────────────

class PredictiveEngine:
    """Phase 4 orchestrator — runs lightweight predictions every tick."""

    PERSIST_FILE = Path.home() / ".hermes" / "helios" / "data" / "predictor_state.json"

    def __init__(self, db):
        self.db = db
        self._last_alerts: list[dict] = []
        self._pushed_ids: set[str] = self._load_pushed_ids()  # survive restarts

    def _load_pushed_ids(self) -> set[str]:
        """Load persisted dedup set from disk."""
        try:
            if self.PERSIST_FILE.exists():
                data = json.loads(self.PERSIST_FILE.read_text())
                return set(data.get("pushed_ids", []))
        except Exception:
            pass
        return set()

    def _save_pushed_ids(self) -> None:
        """Persist dedup set to disk so restarts don't re-fire alerts."""
        try:
            self.PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.PERSIST_FILE.write_text(json.dumps({
                "pushed_ids": sorted(self._pushed_ids),
                "updated": datetime.now(timezone.utc).isoformat(),
            }))
        except Exception as exc:
            log.debug("Failed to persist predictor state: %s", exc)

    def tick_check(self) -> list[dict]:
        """Run all predictors. Returns alerts that should be pushed to Discord."""
        all_alerts: list[dict] = []

        try:
            all_alerts.extend(SleepPredictor.predict(self.db))
            all_alerts.extend(ActivityPredictor.predict(self.db))
            all_alerts.extend(HealthScorePredictor.predict(self.db))
        except Exception as exc:
            log.warning("Prediction tick failed: %s", exc)

        self._last_alerts = all_alerts

        # Only push high-priority, not-previously-pushed
        pushable = []
        for alert in all_alerts:
            alert_id = f"{alert['type']}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
            if alert["priority"] >= 2 and alert_id not in self._pushed_ids:
                pushable.append(alert)
                self._pushed_ids.add(alert_id)

        # Keep set from growing forever
        if len(self._pushed_ids) > 50:
            self._pushed_ids = set(sorted(self._pushed_ids)[-30:])

        # Persist if anything changed
        if pushable:
            self._save_pushed_ids()

        return pushable

    @staticmethod
    def format_alerts(alerts: list[dict]) -> str | None:
        """Format predictive alerts for Discord push."""
        if not alerts:
            return None
        
        lines = ["**🔮 Helios Predictive**"]
        for alert in alerts[:2]:  # max 2 per push
            icon = "🚨" if alert["priority"] >= 3 else "⚠️"
            lines.append(f"\n{icon} **{alert['title']}**\n_{alert['detail']}_")
        
        return "\n".join(lines)
