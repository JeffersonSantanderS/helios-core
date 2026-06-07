"""Helios v5 — Cross-Module Correlation Engine.

Discovers and reports relationships between module data (e.g., protein intake
→ sleep quality → next-day mood). Runs weekly correlation scans comparing pairs
of module metrics over rolling 7/14/28-day windows.

Architecture:
    - CorrelationEngine.run_weekly_scan() is the main entry point
    - Reads daily data from module-specific tables (mood, focus) and
      metric_snapshots (for context-only metrics like protein, sleep)
    - Computes Pearson correlation coefficient with p-value for each pair
    - Stores discovered correlations in the `correlations` table
    - Auto-generates rule suggestions from strong correlations (→ need approval)
    - Integration with weekly briefing via get_top_correlations()

Known pairs always checked:
    protein ↔ sleep, sleep ↔ mood, gaming ↔ sleep,
    screen_time ↔ mood, activity ↔ mood

Pitfalls:
    - Minimum 7 data points required; skip pairs with less
    - Don't over-engineer: simple stats, no ML models
    - Correlation observations are deduplicated by date_key
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("helios.correlator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODULE_NAME = "correlator"
SOURCE = "script_engine"

# Known metric pairs to always check
KNOWN_PAIRS: list[tuple[str, str]] = [
    # Existing pairs
    ("protein.grams_daily", "sleep.hours"),
    ("sleep.hours", "mood.score"),
    ("gaming.minutes_daily", "sleep.hours"),
    ("screen_time.minutes_daily", "mood.score"),
    ("activity.minutes_daily", "mood.score"),
    ("sleep.hours", "activity.minutes_daily"),  # discovered r=0.893, 16 paired dates
    ("resting_heart_rate.avg_daily", "sleep.hours"),  # RHR↔sleep also rich data
    # Weather correlations — temperature affects sleep, mood, activity
    ("weather.temp_max", "sleep.hours"),
    ("weather.temp_max", "mood.score"),
    ("weather.temp_max", "activity.steps_daily"),
    ("weather.precipitation", "sleep.hours"),
    ("weather.precipitation", "activity.steps_daily"),
    ("weather.temp_min", "sleep.hours"),
    # Activity correlations — steps and stand_minutes are richer than exercise_minutes
    ("activity.steps_daily", "sleep.hours"),
    ("activity.stand_minutes", "sleep.hours"),
    ("activity.steps_daily", "mood.score"),
    # Sleep architecture — deep/REM matter for recovery
    ("sleep.deep_hours", "sleep.hours"),
    ("sleep.rem_hours", "mood.score"),
    ("sleep.deep_hours", "health.resting_hr"),
    # Health vitals
    ("health.resting_hr", "sleep.hours"),
    ("health.hrv_ms", "sleep.hours"),
    ("health.blood_o2", "sleep.hours"),
    # Spotify — late listening could affect sleep
    ("spotify.listen_minutes_daily", "sleep.hours"),
    ("spotify.listen_minutes_daily", "mood.score"),
    # Nutrition — beyond protein
    ("nutrition.calories_daily", "sleep.hours"),
    ("nutrition.carbs_daily", "sleep.hours"),
]

# Metric definitions: how to extract daily values
# Each metric specifies a source_type and query:
#   - "module_table": reads from a dedicated module table (mood, focus)
#   - "metric_snapshots": reads from the metric_snapshots table (populated
#     by modules during tick or by the correlator during scans)
METRIC_DEFS: dict[str, dict[str, Any]] = {
    "protein.grams_daily": {
        "source_type": "metric_snapshots",
        "description": "Daily protein intake in grams",
    },
    "sleep.hours": {
        "source_type": "metric_snapshots",
        "description": "Hours of sleep per night",
    },
    "mood.score": {
        "source_type": "module_table",
        "description": "Daily average mood score (1-10)",
        "query": """
            SELECT date(ts) AS day, AVG(CAST(score AS REAL)) AS val
            FROM mood
            WHERE date(ts) >= date('now', ?)
            GROUP BY day ORDER BY day
        """,
    },
    "gaming.minutes_daily": {
        "source_type": "module_table",
        "description": "Daily gaming time in minutes",
        "query": """
            SELECT date(ts) AS day,
                   SUM(CASE WHEN state = 'gaming'
                       THEN COALESCE(duration_secs, 0) / 60.0 ELSE 0 END) AS val
            FROM focus
            WHERE date(ts) >= date('now', ?)
            GROUP BY day ORDER BY day
        """,
    },
    "screen_time.minutes_daily": {
        "source_type": "module_table",
        "description": "Daily total screen time in minutes",
        "query": """
            SELECT date(ts) AS day,
                   SUM(COALESCE(duration_secs, 0)) / 60.0 AS val
            FROM focus
            WHERE state = 'idle' AND date(ts) >= date('now', ?)
            GROUP BY day ORDER BY day
        """,
    },
    "activity.minutes_daily": {
        "source_type": "metric_snapshots",
        "description": "Daily active minutes (Apple Exercise Time — heart-rate-gated)",
    },
    "resting_heart_rate.avg_daily": {
        "source_type": "metric_snapshots",
        "description": "Daily average resting heart rate",
    },
    # Weather metrics (Open-Meteo)
    "weather.temp_max": {
        "source_type": "metric_snapshots",
        "description": "Daily maximum temperature (°C)",
    },
    "weather.temp_min": {
        "source_type": "metric_snapshots",
        "description": "Daily minimum temperature (°C)",
    },
    "weather.precipitation": {
        "source_type": "metric_snapshots",
        "description": "Daily precipitation (mm)",
    },
    # Activity metrics (Health Auto Export via HA)
    "activity.steps_daily": {
        "source_type": "metric_snapshots",
        "description": "Daily step count",
    },
    "activity.stand_minutes": {
        "source_type": "metric_snapshots",
        "description": "Daily stand/movement minutes (actual activity time)",
    },
    # Sleep architecture
    "sleep.deep_hours": {
        "source_type": "metric_snapshots",
        "description": "Daily deep sleep hours",
    },
    "sleep.rem_hours": {
        "source_type": "metric_snapshots",
        "description": "Daily REM sleep hours",
    },
    # Health vitals
    "health.resting_hr": {
        "source_type": "metric_snapshots",
        "description": "Resting heart rate (bpm) — HA sensor",
    },
    "health.hrv_ms": {
        "source_type": "metric_snapshots",
        "description": "Heart rate variability (ms)",
    },
    "health.blood_o2": {
        "source_type": "metric_snapshots",
        "description": "Blood oxygen saturation (%)",
    },
    # Spotify
    "spotify.listen_minutes_daily": {
        "source_type": "metric_snapshots",
        "description": "Daily Spotify listening minutes",
    },
    # Nutrition
    "nutrition.calories_daily": {
        "source_type": "metric_snapshots",
        "description": "Daily calorie intake",
    },
    "nutrition.carbs_daily": {
        "source_type": "metric_snapshots",
        "description": "Daily carbohydrate intake (g)",
    },
}

# Strength thresholds for Pearson r
STRENGTH_WEAK = 0.3
STRENGTH_MODERATE = 0.5


# ---------------------------------------------------------------------------
# Statistical helpers (no external deps)
# ---------------------------------------------------------------------------

def _pearson_r(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Compute Pearson correlation coefficient and approximate p-value.

    Returns (r, p) where:
        r = Pearson correlation coefficient (-1 to 1)
        p = two-tailed p-value using t-distribution approximation

    Requires len(xs) == len(ys) and >= 7 data points.
    Returns (0.0, 1.0) for insufficient data or zero variance.
    """
    n = len(xs)
    if n < 7 or len(ys) != n:
        return 0.0, 1.0

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)

    if sxx == 0 or syy == 0:
        return 0.0, 1.0

    r = sxy / math.sqrt(sxx * syy)
    r = max(-1.0, min(1.0, r))

    r_sq = r * r
    if r_sq >= 1.0:
        return r, 0.0

    df = n - 2
    t_stat = abs(r) * math.sqrt(df / (1.0 - r_sq))
    p = _t_distribution_p(t_stat, df)
    return r, p


def _t_distribution_p(t: float, df: int) -> float:
    """Approximate two-tailed p-value from t-distribution."""
    if df <= 0 or t <= 0:
        return 1.0

    if df >= 30:
        z = t * (1 - 1.0 / (4 * df)) / math.sqrt(1 + t * t / (2 * df))
        return 2.0 * (1.0 - _normal_cdf(abs(z)))

    x = df / (df + t * t)
    p = _regularized_incomplete_beta(0.5 * df, 0.5, x)
    return min(1.0, 2.0 * p)


def _normal_cdf(z: float) -> float:
    """Approximate standard normal CDF using Abramowitz & Stegun."""
    if z < -8:
        return 0.0
    if z > 8:
        return 1.0
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = 1 if z >= 0 else -1
    z_abs = abs(z)
    t = 1.0 / (1.0 + p * z_abs)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-z_abs * z_abs / 2)
    return 0.5 * (1.0 + sign * y)


def _log_gamma(x: float) -> float:
    """Lanczos approximation of log(Gamma(x))."""
    if x <= 0:
        return 0.0
    coefficients = [
        76.18009172947146, -86.50532032941677, 24.01409824083091,
        -1.231739572450155, 0.001208650973866179, -5.395239384953e-6,
    ]
    temp = x + 5.5
    temp -= (x + 0.5) * math.log(temp)
    ser = 1.000000000190015
    for i, c in enumerate(coefficients):
        ser += c / (x + i + 1)
    return -temp + math.log(2.5066282746310005 * ser / x)


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """Approximate the regularized incomplete beta function I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    if x > (a + 1) / (a + b + 2):
        return 1.0 - _regularized_incomplete_beta(b, a, 1.0 - x)

    log_beta = _log_gamma(a) + _log_gamma(b) - _log_gamma(a + b)
    front = math.exp(a * math.log(x) + b * math.log(1 - x) - log_beta)

    tiny = 1e-30
    f = tiny
    c = tiny
    d = 0.0

    for m in range(1, 201):
        if m == 1:
            numerator = 1.0
        elif m % 2 == 0:
            mm = m // 2
            numerator = (mm * (b - mm) * x) / ((a + 2 * mm - 1) * (a + 2 * mm))
        else:
            mm = (m - 1) // 2
            numerator = -((a + mm) * (a + b + mm) * x) / ((a + 2 * mm) * (a + 2 * mm + 1))

        d = numerator * f + d
        if abs(d) < tiny:
            d = tiny
        d = 1.0 / d

        c = numerator * f + c
        if abs(c) < tiny:
            c = tiny

        delta = c * d
        f *= delta

        if abs(delta - 1.0) < 1e-10:
            break

    return front * f / a


def _classify_strength(abs_r: float) -> str:
    """Classify correlation strength from absolute r value."""
    if abs_r >= STRENGTH_MODERATE:
        return "strong"
    if abs_r >= STRENGTH_WEAK:
        return "moderate"
    return "weak"


def _classify_direction(r: float) -> str:
    """Classify correlation direction."""
    return "positive" if r >= 0 else "negative"


# ---------------------------------------------------------------------------
# CorrelationEngine
# ---------------------------------------------------------------------------

class CorrelationEngine:
    """Helios v5 Cross-Module Correlation Engine.

    Discovers relationships between module metrics using Pearson correlation
    over rolling time windows. Stores results in the correlations table and
    generates rule suggestions for strong correlations.

    Parameters:
        db_path: Path to the SQLite database.
        config: Module configuration dict. Expected keys:
            - min_data_points (int): Minimum paired observations needed (default 7)
            - strong_threshold (float): Pearson r threshold for 'strong' (default 0.7)
            - significance_threshold (float): p-value threshold (default 0.05)
            - scan_windows (list[int]): Rolling windows to scan (default [7, 14, 28])
            - min_days_data (int): Minimum days of data before any suggestions (default 14)
    """

    def __init__(self, db_path: str, config: Optional[dict] = None) -> None:
        self.db_path = db_path
        self.config = config or {}
        self._min_points: int = self.config.get("min_data_points", 7)
        self._strong_threshold: float = self.config.get("strong_threshold", 0.7)
        self._significance: float = self.config.get("significance_threshold", 0.05)
        self._scan_windows: list[int] = self.config.get("scan_windows", [7, 14, 28])
        self._min_days_data: int = self.config.get("min_days_data", 5)  # lowered from 14 — start finding patterns sooner
        self._ensure_tables()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_tables(self) -> None:
        """Create correlation-related tables if they don't exist."""
        migrations_dir = Path(__file__).parent / "migrations"
        migration_file = migrations_dir / "004_correlations.sql"
        if migration_file.exists():
            try:
                conn = self._get_conn()
                conn.executescript(migration_file.read_text())
                conn.close()
            except sqlite3.Error as exc:
                logger.error("Failed to ensure correlations tables: %s", exc)

    # ------------------------------------------------------------------
    # Data collection
    # ------------------------------------------------------------------

    def _fetch_metric_data(self, conn: sqlite3.Connection, metric: str,
                           days_back: int) -> dict[str, float]:
        """Fetch daily aggregated data for a metric over the given window.

        For module_table metrics, queries the dedicated table directly.
        For metric_snapshots metrics, queries the snapshots table.

        Returns:
            Dict of {date_key: value} for each day with data.
        """
        metric_def = METRIC_DEFS.get(metric)
        if not metric_def:
            logger.warning("No definition for metric: %s", metric)
            return {}

        source_type = metric_def["source_type"]
        window_param = f"-{days_back} days"

        try:
            if source_type == "module_table":
                query = metric_def["query"]
                rows = conn.execute(query, (window_param,)).fetchall()
            elif source_type == "metric_snapshots":
                query = """
                    SELECT date_key AS day, value AS val
                    FROM metric_snapshots
                    WHERE metric = ? AND date_key >= date('now', ?)
                    ORDER BY day
                """
                rows = conn.execute(query, (metric, window_param)).fetchall()
                conn.row_factory = sqlite3.Row
            else:
                logger.warning("Unknown source_type for metric %s: %s", metric, source_type)
                return {}

            return {row["day"]: row["val"] for row in rows if row["val"] is not None}
        except sqlite3.Error as exc:
            logger.error("Failed to fetch metric %s: %s", metric, exc)
            return {}

    def _pair_observations(self, data_a: dict[str, float],
                           data_b: dict[str, float]) -> tuple[list[float], list[float]]:
        """Pair two metrics by date key, returning aligned (xs, ys) lists."""
        common_dates = set(data_a.keys()) & set(data_b.keys())
        if not common_dates:
            return [], []
        sorted_dates = sorted(common_dates)
        xs = [data_a[d] for d in sorted_dates]
        ys = [data_b[d] for d in sorted_dates]
        return xs, ys

    def _store_observations(self, conn: sqlite3.Connection,
                            metric_a: str, metric_b: str,
                            data_a: dict[str, float],
                            data_b: dict[str, float]) -> int:
        """Store paired observations in correlation_observations table.

        Returns count of observations stored.
        """
        common_dates = set(data_a.keys()) & set(data_b.keys())
        count = 0
        for date_key in common_dates:
            try:
                conn.execute(
                    """INSERT INTO correlation_observations (metric_a, metric_b, value_a, value_b, date_key)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (metric_a, metric_b, date_key) DO UPDATE SET
                         value_a = excluded.value_a,
                         value_b = excluded.value_b,
                         ts = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    """,
                    (metric_a, metric_b, data_a[date_key], data_b[date_key], date_key),
                )
                count += 1
            except sqlite3.Error as exc:
                logger.error("Failed to store observation %s↔%s for %s: %s",
                             metric_a, metric_b, date_key, exc)
        return count

    # ------------------------------------------------------------------
    # Core correlation scan
    # ------------------------------------------------------------------

    def _compute_correlation(self, metric_a: str, metric_b: str,
                             days_back: int,
                             conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        """Compute correlation between two metrics over a rolling window.

        Returns:
            Dict with correlation results, or None if insufficient data.
        """
        data_a = self._fetch_metric_data(conn, metric_a, days_back)
        data_b = self._fetch_metric_data(conn, metric_b, days_back)

        # Store raw observations for audit
        self._store_observations(conn, metric_a, metric_b, data_a, data_b)

        # Align and pair
        xs, ys = self._pair_observations(data_a, data_b)
        if len(xs) < self._min_points:
            logger.debug(
                "Insufficient data for %s↔%s (window=%dd): need %d, got %d",
                metric_a, metric_b, days_back, self._min_points, len(xs),
            )
            return None

        r, p = _pearson_r(xs, ys)
        strength = _classify_strength(abs(r))
        direction = _classify_direction(r)

        logger.info(
            "Correlation %s↔%s (window=%dd): r=%.3f, p=%.4f, strength=%s, direction=%s, n=%d",
            metric_a, metric_b, days_back, r, p, strength, direction, len(xs),
        )

        return {
            "metric_a": metric_a,
            "metric_b": metric_b,
            "window_days": days_back,
            "pearson_r": r,
            "p_value": p,
            "strength": strength,
            "direction": direction,
            "n_observations": len(xs),
            "is_significant": p < self._significance,
        }

    def _maybe_suggest_rule(self, corr: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Generate a rule suggestion from a strong, significant correlation."""
        abs_r = abs(corr["pearson_r"])
        if abs_r < self._strong_threshold or corr["p_value"] >= self._significance:
            return None

        direction = "increases" if corr["direction"] == "positive" else "decreases"
        metric_a = corr["metric_a"].replace("_", " ").replace(".", " ")
        metric_b = corr["metric_b"].replace("_", " ").replace(".", " ")

        slug = f"corr_{corr['metric_a'].replace('.', '_')}_{corr['direction'][:4]}_{corr['metric_b'].replace('.', '_')}"

        rule = {
            "slug": slug,
            "trigger_type": "pattern",
            "trigger_config": {
                "metric": corr["metric_a"],
                "direction": corr["direction"],
                "window_days": corr["window_days"],
            },
            "condition": f"ABS({corr['metric_a']}.change) > 0 AND correlated with {corr['metric_b']}",
            "action_type": "llm_request",
            "action_config": {
                "action": "notify",
                "message": (
                    f"When {metric_a} {direction}, {metric_b} tends to change "
                    f"(r={corr['pearson_r']:.2f}, {corr['window_days']}d window)"
                ),
            },
            "description": (
                f"Pattern: {metric_a} {direction} → {metric_b} "
                f"(r={corr['pearson_r']:.2f}, p={corr['p_value']:.3f}, "
                f"n={corr['n_observations']}, {corr['window_days']}-day window)"
            ),
            "correlation_evidence": {
                "pearson_r": corr["pearson_r"],
                "p_value": corr["p_value"],
                "n_observations": corr["n_observations"],
                "window_days": corr["window_days"],
            },
        }
        return rule

    def _store_correlation(self, conn: sqlite3.Connection,
                          corr: dict[str, Any]) -> None:
        """Upsert a correlation record into the database."""
        suggested_rule = corr.get("suggested_rule")
        rule_json = json.dumps(suggested_rule) if suggested_rule else None

        try:
            conn.execute(
                """INSERT INTO correlations
                   (metric_a, metric_b, window_days, pearson_r, p_value,
                    strength, direction, n_observations, suggested_rule, approved)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                   ON CONFLICT (metric_a, metric_b, window_days) DO UPDATE SET
                     pearson_r = excluded.pearson_r,
                     p_value = excluded.p_value,
                     strength = excluded.strength,
                     direction = excluded.direction,
                     n_observations = excluded.n_observations,
                     suggested_rule = excluded.suggested_rule,
                     ts = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                     updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (
                    corr["metric_a"],
                    corr["metric_b"],
                    corr["window_days"],
                    corr["pearson_r"],
                    corr["p_value"],
                    corr["strength"],
                    corr["direction"],
                    corr["n_observations"],
                    rule_json,
                ),
            )
        except sqlite3.Error as exc:
            logger.error("Failed to store correlation %s↔%s: %s",
                         corr["metric_a"], corr["metric_b"], exc)

    # ------------------------------------------------------------------
    # Metric snapshot management
    # ------------------------------------------------------------------

    def snapshot_metric(self, metric: str, value: float,
                        date_key: Optional[str] = None) -> None:
        """Store a daily snapshot value for a metric.

        This is the primary way to populate time-series data for metrics
        that don't have dedicated module tables (protein, sleep, activity).
        Can be called during module ticks or by external data collectors.
        """
        if date_key is None:
            date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO metric_snapshots (metric, value, date_key, source)
                   VALUES (?, ?, ?, 'correlator')
                   ON CONFLICT (metric, date_key) DO UPDATE SET
                     value = excluded.value,
                     ts = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (metric, value, date_key),
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to snapshot metric %s: %s", metric, exc)
        finally:
            conn.close()

    def snapshot_from_context(self, module: str, key: str, metric: str,
                              value_path: str = None) -> None:
        """Extract a value from the context table and store as a daily snapshot.

        Args:
            module: Context module name (e.g., 'protein')
            key: Context key (e.g., 'daily_summary')
            metric: Target metric name (e.g., 'protein.grams_daily')
            value_path: JSON path to extract value (e.g., '$.current_grams').
                        If None, uses the full context value as a number.
        """
        date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        conn = self._get_conn()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT value FROM context WHERE module = ? AND key = ? ORDER BY ts DESC LIMIT 1",
                (module, key),
            ).fetchone()

            if not row:
                logger.debug("No context found for %s.%s", module, key)
                return

            try:
                value_data = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                value_data = row["value"]

            if value_path and isinstance(value_data, dict):
                # Extract nested value using path like 'current_grams'
                path_parts = value_path.lstrip("$.").split(".")
                for part in path_parts:
                    if isinstance(value_data, dict) and part in value_data:
                        value_data = value_data[part]
                    else:
                        logger.debug("Path %s not found in context value", value_path)
                        return
                value = float(value_data)
            else:
                value = float(value_data)

            conn.execute(
                """INSERT INTO metric_snapshots (metric, value, date_key, source)
                   VALUES (?, ?, ?, 'context_snapshot')
                   ON CONFLICT (metric, date_key) DO UPDATE SET
                     value = excluded.value,
                     ts = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (metric, value, date_key),
            )
            conn.commit()

        except (ValueError, TypeError) as exc:
            logger.error("Failed to snapshot context %s.%s → %s: %s",
                         module, key, metric, exc)
        except sqlite3.Error as exc:
            logger.error("DB error snapshotting context %s.%s: %s", module, key, exc)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_weekly_scan(self, pairs: Optional[list[tuple[str, str]]] = None,
                       windows: Optional[list[int]] = None) -> list[dict[str, Any]]:
        """Run a weekly correlation scan across all known metric pairs.

        Before computing correlations, snapshots context-only metrics so they
        have daily time-series data available.

        Returns:
            List of correlation result dicts.
        """
        # Check minimum data age from metric_snapshots (the ingestion source)
        conn = self._get_conn()
        try:
            # Snapshot context-only metrics first
            self._snapshot_context_metrics(conn)

            # Check if we have enough days of data — use metric_snapshots date_key
            snap_row = conn.execute(
                "SELECT MIN(date_key) as earliest FROM metric_snapshots"
            ).fetchone()
            if snap_row and snap_row["earliest"]:
                try:
                    snap_earliest = datetime.strptime(snap_row["earliest"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    snap_days = (datetime.now(timezone.utc) - snap_earliest).days
                    if snap_days < self._min_days_data:
                        logger.info(
                            "Only %d days of snapshots (need %d) — skipping correlation scan",
                            snap_days, self._min_days_data,
                        )
                        return []
                except (ValueError, TypeError):
                    pass
        except sqlite3.Error:
            pass

        pairs = pairs or KNOWN_PAIRS
        windows = windows or self._scan_windows
        results: list[dict[str, Any]] = []

        for metric_a, metric_b in pairs:
            for window in windows:
                corr = self._compute_correlation(metric_a, metric_b, window, conn)
                if corr is None:
                    continue

                # Generate rule suggestion for strong correlations
                suggested_rule = self._maybe_suggest_rule(corr)
                if suggested_rule:
                    corr["suggested_rule"] = suggested_rule

                self._store_correlation(conn, corr)
                results.append(corr)

        conn.commit()
        conn.close()

        logger.info("Weekly scan complete: %d correlations found", len(results))
        return results

    def run_daily_scan(self) -> list[dict[str, Any]]:
        """Lightweight daily re-scan on top 3 most valuable pairs (14-day window only).

        Weekly scans cover all pairs × all windows. This runs every day
        on just the pairs that matter most for tight feedback loops:
        sleep↔activity, sleep↔mood, activity↔mood.

        Returns list of correlation results (0-3 items).
        """
        DAILY_PAIRS: list[tuple[str, str]] = [
            ("sleep.hours", "activity.minutes_daily"),
            ("sleep.hours", "mood.score"),
            ("activity.minutes_daily", "mood.score"),
        ]

        conn = self._get_conn()
        results: list[dict[str, Any]] = []

        try:
            for metric_a, metric_b in DAILY_PAIRS:
                corr = self._compute_correlation(metric_a, metric_b, 14, conn)
                if corr is None:
                    continue
                self._store_correlation(conn, corr)
                results.append(corr)

            conn.commit()
        except Exception as exc:
            logger.warning("Daily scan failed: %s", exc)
        finally:
            conn.close()

        if results:
            logger.info("Daily scan: %d correlations refreshed", len(results))
        return results

    def _snapshot_context_metrics(self, conn: sqlite3.Connection) -> None:
        """Take daily snapshots of context-only metrics for time-series analysis.

        Reads the latest value from context for each metric_snapshots metric
        and stores it with today's date_key.
        """
        # Map context-only metrics to their context keys
        context_metric_map = {
            "protein.grams_daily": {
                "module": "protein",
                "key": "daily_summary",
                "value_path": "current_grams",
            },
            "sleep.hours": {
                "module": "health",
                "key": "sleep",
                "value_path": "hours",
            },
            "activity.minutes_daily": {
                "module": "health",
                "key": "activity",
                "value_path": "active_minutes",
            },
        }

        date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for metric, mapping in context_metric_map.items():
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT value FROM context WHERE module = ? AND key = ? ORDER BY ts DESC LIMIT 1",
                    (mapping["module"], mapping["key"]),
                ).fetchone()

                if not row:
                    continue

                try:
                    value_data = json.loads(row["value"])
                except (json.JSONDecodeError, TypeError):
                    continue

                # Extract nested value
                path_parts = mapping["value_path"].split(".")
                current = value_data
                for part in path_parts:
                    if isinstance(current, dict) and part in current:
                        current = current[part]
                    else:
                        break

                try:
                    value = float(current)
                    conn.execute(
                        """INSERT INTO metric_snapshots (metric, value, date_key, source)
                           VALUES (?, ?, ?, 'context_snapshot')
                           ON CONFLICT (metric, date_key) DO UPDATE SET
                             value = excluded.value,
                             ts = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        """,
                        (metric, value, date_key),
                    )
                except (ValueError, TypeError):
                    continue

            except sqlite3.Error:
                continue

    def get_top_correlations(self, limit: int = 3,
                             window: Optional[int] = None,
                             min_strength: str = "moderate") -> list[dict[str, Any]]:
        """Get the top N strongest correlations for briefing inclusion."""
        strength_order = {"weak": 0, "moderate": 1, "strong": 2}
        min_level = strength_order.get(min_strength, 1)

        conn = self._get_conn()
        try:
            sql = "SELECT * FROM correlations WHERE 1=1"
            params: list = []

            allowed_strengths = [s for s, level in strength_order.items() if level >= min_level]
            placeholders = ",".join("?" * len(allowed_strengths))
            sql += f" AND strength IN ({placeholders})"
            params.extend(allowed_strengths)

            if window is not None:
                sql += " AND window_days = ?"
                params.append(window)

            sql += " ORDER BY ABS(pearson_r) DESC, p_value ASC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            logger.error("Failed to get top correlations: %s", exc)
            return []
        finally:
            conn.close()

    def get_correlation(self, metric_a: str, metric_b: str,
                        window: Optional[int] = None) -> Optional[dict[str, Any]]:
        """Get the latest correlation between two specific metrics."""
        conn = self._get_conn()
        try:
            if window is not None:
                row = conn.execute(
                    "SELECT * FROM correlations WHERE metric_a = ? AND metric_b = ? AND window_days = ?",
                    (metric_a, metric_b, window),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM correlations WHERE metric_a = ? AND metric_b = ? ORDER BY ABS(pearson_r) DESC LIMIT 1",
                    (metric_a, metric_b),
                ).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as exc:
            logger.error("Failed to get correlation %s↔%s: %s", metric_a, metric_b, exc)
            return None
        finally:
            conn.close()

    def approve_correlation_rule(self, correlation_id: int,
                                 approved_by: str = "user") -> bool:
        """Approve a correlation's suggested rule for activation."""
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE correlations SET approved = 1, approved_by = ? WHERE id = ?",
                (approved_by, correlation_id),
            )
            conn.commit()
            return True
        except sqlite3.Error as exc:
            logger.error("Failed to approve correlation %d: %s", correlation_id, exc)
            return False
        finally:
            conn.close()

    def format_briefing_section(self, correlations: list[dict[str, Any]]) -> str:
        """Format correlations as a human-readable briefing section."""
        if not correlations:
            return "No significant patterns detected this week."

        lines = []
        for i, corr in enumerate(correlations, 1):
            r = corr["pearson_r"]
            p = corr["p_value"]
            n = corr["n_observations"]
            window = corr["window_days"]
            strength = corr["strength"]
            direction = corr["direction"]
            metric_a = corr["metric_a"].split(".")[-1].replace("_", " ")
            metric_b = corr["metric_b"].split(".")[-1].replace("_", " ")

            arrow = "📈" if direction == "positive" else "📉"
            lines.append(
                f"{i}. {arrow} **{metric_a}** ↔ **{metric_b}**: "
                f"r={r:.2f} ({strength}, {window}d, n={n})"
            )
        return "\n".join(lines)

    def status(self) -> dict[str, Any]:
        """Return engine status for health checks."""
        conn = self._get_conn()
        try:
            count = conn.execute("SELECT COUNT(*) FROM correlations").fetchone()[0]
            obs_count = conn.execute("SELECT COUNT(*) FROM correlation_observations").fetchone()[0]
            snap_count = conn.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0]
        except sqlite3.Error:
            count = 0
            obs_count = 0
            snap_count = 0
        finally:
            conn.close()

        return {
            "module": MODULE_NAME,
            "known_pairs": len(KNOWN_PAIRS),
            "scan_windows": self._scan_windows,
            "min_data_points": self._min_points,
            "strong_threshold": self._strong_threshold,
            "significance_threshold": self._significance,
            "correlations_stored": count,
            "observations_stored": obs_count,
            "snapshots_stored": snap_count,
            "db_path": self.db_path,
        }