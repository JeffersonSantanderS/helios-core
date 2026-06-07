"""Helios v6 — Outcome Tracker.

Records predictions made by the predictive engine,
then compares against actual outcomes when new data arrives.
Closes the open-loop regression with a feedback signal.

Architecture:
  - log_prediction(): called when a prediction alert is pushed
  - evaluate_predictions(): runs daily — reads today's actuals,
    compares against stored predictions, computes accuracy
  - Stores results in the SQLite 'prediction_outcomes' table
"""

from __future__ import annotations

import json, logging, sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("helios.outcomes")

# ── Prediction horizon to outcome window mapping ─────────────────────────
# Predictions for N days ahead get evaluated N days later
EVAL_WINDOW_DAYS = 7  # evaluate predictions up to 7 days old


class OutcomeTracker:
    """Records and evaluates prediction accuracy over time."""

    def __init__(self, db):
        self.db = db
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create prediction_outcomes table if missing."""
        conn = self.db._conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prediction_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prediction_ts TEXT NOT NULL,
                    eval_ts TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    days_ahead INTEGER NOT NULL,
                    predicted_value REAL NOT NULL,
                    low_bound REAL,
                    actual_value REAL,
                    error REAL,
                    abs_pct_error REAL,
                    within_bounds INTEGER,  -- 1 if actual within [low_bound, predicted]
                    trend_slope REAL,
                    r_squared REAL,
                    days_data INTEGER,
                    resolved INTEGER DEFAULT 0  -- 0=pending, 1=evaluated
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_prediction_outcomes_metric
                ON prediction_outcomes(metric, prediction_ts)
            """)
        except Exception as exc:
            log.debug("Outcome table: %s", exc)

    def log_prediction(self, alert: dict) -> None:
        """Record a prediction that was pushed to Discord."""
        conn = self.db._conn()
        try:
            metric = alert.get("type", "").replace("_projection", "").replace("_danger", "").replace("_decline", "")
            projected = alert.get("projected")
            low_bound = alert.get("low_bound")
            days_ahead = alert.get("days_ahead", 3)
            slope = alert.get("trend_slope")
            days_data = alert.get("days_data")

            # Map alert type to real metric name
            # Alert types from predictor.py: sleep_projection_danger, activity_projection_low, health_score_projection
            metric_map = {
                "sleep": "sleep.hours",
                "sleep_projection_danger": "sleep.hours",
                "sleep_projection_decline": "sleep.hours",
                "activity": "activity.minutes_daily",
                "activity_projection_low": "activity.minutes_daily",
                "health_score": "health_score.total",
                "health_score_projection": "health_score.total",
            }
            real_metric = metric_map.get(metric, metric)

            conn.execute(
                """INSERT INTO prediction_outcomes
                   (prediction_ts, eval_ts, metric, days_ahead, predicted_value,
                    low_bound, trend_slope, days_data, resolved)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    (datetime.now(timezone.utc) + timedelta(days=days_ahead)).isoformat(),
                    real_metric,
                    days_ahead,
                    projected,
                    low_bound,
                    slope,
                    days_data,
                ),
            )
            conn.commit()
            log.info("Prediction logged: %s → %.2f in %dd", real_metric, projected, days_ahead)
        except Exception as exc:
            log.debug("Log prediction failed: %s", exc)

    def evaluate_predictions(self) -> dict:
        """Compare pending predictions against actual metric values.
        Returns summary dict. Runs once per day per metric."""
        conn = self.db._conn()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = {"evaluated": 0, "accurate": 0, "total_error": 0.0, "details": []}

        try:
            # Find predictions due for evaluation (prediction date + days_ahead <= today)
            cur = conn.execute(
                """SELECT id, metric, predicted_value, low_bound, days_ahead, prediction_ts
                   FROM prediction_outcomes
                   WHERE resolved = 0 AND date(eval_ts) <= ?
                   ORDER BY prediction_ts""",
                (today,)
            )
            pending = list(cur.fetchall())

            for row in pending:
                pid, metric, predicted, low_bound, days_ahead, pred_ts = row

                # Get actual value
                pred_date = pred_ts[:10]
                # The actual is the value on the date the prediction was about
                eval_date = (datetime.fromisoformat(pred_ts) + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

                cur = conn.execute(
                    "SELECT value FROM metric_snapshots WHERE metric=? AND date_key=?",
                    (metric, eval_date)
                )
                actual_row = cur.fetchone()
                if not actual_row or actual_row[0] is None:
                    continue  # no data yet for this date

                actual = actual_row[0]
                error = predicted - actual
                abs_pct = (abs(error) / max(abs(actual), 0.01)) * 100
                within = 1 if (low_bound is None or (low_bound <= actual <= predicted + (predicted * 0.1))) else 0

                conn.execute(
                    """UPDATE prediction_outcomes
                       SET actual_value=?, error=?, abs_pct_error=?, within_bounds=?,
                           resolved=1
                       WHERE id=?""",
                    (actual, error, round(abs_pct, 1), within, pid),
                )

                result["evaluated"] += 1
                if abs_pct <= 20:  # within 20% = accurate
                    result["accurate"] += 1
                result["total_error"] += abs_pct
                result["details"].append({
                    "metric": metric,
                    "predicted": round(predicted, 2),
                    "actual": round(actual, 2),
                    "error": round(error, 2),
                    "abs_pct_error": round(abs_pct, 1),
                    "days_ahead": days_ahead,
                })

            conn.commit()

            if result["evaluated"] > 0:
                avg_error = round(result["total_error"] / result["evaluated"], 1)
                accuracy_pct = round(result["accurate"] / result["evaluated"] * 100)
                log.info(
                    "Outcome evaluation: %d predictions, %d%% accurate, avg error %.1f%%",
                    result["evaluated"], accuracy_pct, avg_error
                )

        except Exception as exc:
            log.warning("Outcome evaluation failed: %s", exc)

        return result

    def get_accuracy_summary(self) -> dict:
        """Return historical accuracy stats for the predictor."""
        conn = self.db._conn()
        try:
            cur = conn.execute(
                """SELECT metric, COUNT(*) as n,
                          ROUND(AVG(abs_pct_error), 1) as avg_error,
                          ROUND(AVG(within_bounds) * 100, 1) as bound_pct
                   FROM prediction_outcomes
                   WHERE resolved=1
                   GROUP BY metric"""
            )
            rows = list(cur.fetchall())
            return {
                "total_evaluated": sum(r[1] for r in rows),
                "by_metric": {r[0]: {"n": r[1], "avg_error_pct": r[2], "within_bounds_pct": r[3]} for r in rows},
            }
        except Exception:
            return {"total_evaluated": 0, "by_metric": {}}
