"""Helios v6 — Memory + Preference Engine.

Phase 1 of the Helios Memory + Preference Layer.
Implements:

  1. Explicit preferences  — user-controlled, lives at data/preferences.json
  2. Inferred patterns      — learned from metric_snapshots, focus, mood, spotify
  3. Confidence scoring     — every pattern tagged with {confidence, samples, last_updated}

Design contract:
  - Explicit prefs NEVER mutated by inference.
  - Inferred patterns always carrying confidence scores.
  - Overrideable: explicit prefs trump inferred.
  - Inspectable: both files are readable JSON.
  - Testable: no hidden state, no LLM dependency.
  - Thread-safe: RLock protects all state mutations.
  - Validated: set() checks value types against known schemas.

Hardened 2026-05-09 — review blockers from ChatGPT resolved:
  1. RLock replaces Lock — no more nested-acquire deadlocks.
  2. First-run cooldown seeded from file mtime or current time.
  3. Gaming timezone uses proper datetime → MDT conversion.
  4. Confidence floor removed — weak patterns stay weak.
  5. Preference validation layer added — type-checking on set().
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

logger = logging.getLogger("helios.preference")

# ── Timezone ────────────────────────────────────────────────────────────────
LOCAL_TZ = ZoneInfo("America/Edmonton")  # DST-aware Mountain time

# ── File paths ──────────────────────────────────────────────────────────────
HELIOS_DATA = Path.home() / ".hermes" / "helios" / "data"
PREFS_FILE    = HELIOS_DATA / "preferences.json"
PATTERNS_FILE = HELIOS_DATA / "inferred_patterns.json"

# ── Confidence thresholds ───────────────────────────────────────────────────
CONFIDENCE_MIN      = 0.3   # discard / label-weak below this
CONFIDENCE_LOW      = 0.5   # mark as "low confidence"
CONFIDENCE_MODERATE = 0.7   # mark as "moderate"
CONFIDENCE_HIGH     = 0.85  # mark as "high"
SAMPLES_MIN         = 3     # don't infer with <3 data points
REFRESH_INTERVAL    = 3600  # seconds between pattern recomputation

# ── Default preferences (seeded on first run) ───────────────────────────────
DEFAULT_PREFS: dict[str, Any] = {
    "quiet_hours":               {"start": "22:00", "end": "09:00"},
    "briefing_priority":         ["sleep", "calendar", "weather", "activity", "mood", "patterns"],
    "cpu_warning_threshold":     85,
    "cpu_critical_threshold":    95,
    "mood_prompt_frequency":     "daily",
    "focus_work_threshold_h":    1.0,
    "gaming_marathon_h":         2.0,
    "low_sleep_alert_h":         5.0,
    "interrupt_defer_work":      True,
    "interrupt_gaming_aware":    True,
    "spotify_heavy_minutes":     120,
    "activity_low_minutes":      20,
    "evening_hour_start":        21,
    "morning_hour_start":        7,
}

# ── Validation schema — key → expected type ─────────────────────────────────
_PREF_SCHEMA: dict[str, type | tuple] = {
    "quiet_hours":               dict,
    "briefing_priority":         list,
    "cpu_warning_threshold":     (int, float),
    "cpu_critical_threshold":    (int, float),
    "mood_prompt_frequency":     str,
    "focus_work_threshold_h":    (int, float),
    "gaming_marathon_h":         (int, float),
    "low_sleep_alert_h":         (int, float),
    "interrupt_defer_work":      bool,
    "interrupt_gaming_aware":    bool,
    "spotify_heavy_minutes":     (int, float),
    "activity_low_minutes":      (int, float),
    "evening_hour_start":        (int, float),
    "morning_hour_start":        (int, float),
}

# ── Quiet-hours sub-schema ──────────────────────────────────────────────────
_QH_SCHEMA: dict[str, type] = {"start": str, "end": str}


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_local() -> datetime:
    """Current time in America/Edmonton (DST-aware)."""
    return datetime.now(LOCAL_TZ)


def _today_utc() -> str:
    return _now_utc().strftime("%Y-%m-%d")


def _days_ago(n: int) -> str:
    return (_now_utc() - timedelta(days=n)).strftime("%Y-%m-%d")


def _local_hour(utc_ts: str) -> int:
    """Extract the local (MDT/MST) hour from a UTC timestamp string.

    Accepts ISO-8601 with optional 'Z' or '+00:00' suffix.
    """
    ts = utc_ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts)
    return dt.astimezone(LOCAL_TZ).hour


def _validate_time_str(value: str) -> bool:
    """True if value is 'HH:MM' in 0-23:0-59 range."""
    try:
        parts = value.split(":")
        if len(parts) != 2:
            return False
        h, m = int(parts[0]), int(parts[1])
        return 0 <= h <= 23 and 0 <= m <= 59
    except (ValueError, TypeError):
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Preference Engine
# ═══════════════════════════════════════════════════════════════════════════

class PreferenceEngine:
    """Singleton preference + inference engine.

    Load explicit prefs from disk once.  Runs periodic inference against
    live metric_snapshots + focus data to build confidence-weighted
    behavioral patterns.

    Thread-safe: uses re-entrant lock so callers nesting load/save/set
    inside refresh_patterns or tick paths never deadlock.

    Usage in engine.py::

        prefs = PreferenceEngine(db_path)
        prefs.load()

        # Later, in tick loop:
        prefs.maybe_refresh_patterns()

        # Query:
        threshold = prefs.get("cpu_warning_threshold", 85)
        quiet  = prefs.is_quiet_hours()
        pattern = prefs.pattern("typical_sleep_hours")
    """

    _lock = threading.RLock()  # re-entrant — fixes ChatGPT review blockers 1–2

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path
        self._explicit: dict[str, Any] = {}
        self._patterns: dict[str, Any] = {}
        self._pattern_ts: float = 0.0
        self._loaded = False

    # ── Load / Save ─────────────────────────────────────────────────────

    def load(self) -> dict[str, Any]:
        """Load explicit preferences + inferred patterns from disk.

        Seeds _pattern_ts from patterns file mtime so first tick
        after restart doesn't re-run inference immediately.
        """
        with self._lock:
            HELIOS_DATA.mkdir(parents=True, exist_ok=True)
            self._explicit = self._load_json(PREFS_FILE, DEFAULT_PREFS.copy())
            self._patterns = self._load_json(PATTERNS_FILE, {})
            self._loaded = True

            # Seed cooldown timer so restart doesn't trigger immediate inference
            # (ChatGPT review blocker 3)
            if PATTERNS_FILE.exists() and self._patterns:
                self._pattern_ts = PATTERNS_FILE.stat().st_mtime
            else:
                self._pattern_ts = _now_utc().timestamp()

            logger.info("Preferences loaded: %d explicit, %d patterns (next refresh in %ds)",
                         len(self._explicit), len(self._patterns),
                         int(self._pattern_ts + REFRESH_INTERVAL - _now_utc().timestamp()))
            return self._explicit

    def _load_json(self, path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            self._save_json(path, default)
            return default
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                return default
            # Merge missing defaults (don't silently lose new pref keys)
            for k, v in default.items():
                if k not in data:
                    data[k] = v
            return data
        except Exception:
            logger.warning("Corrupt %s — re-seeding defaults", path.name)
            self._save_json(path, default)
            return default

    def _save_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(path)

    # ── Lock-aware persistence helpers (prevent RLock from hiding bugs too) ─

    def _save_patterns_unlocked(self) -> None:
        """Write patterns to disk + update timestamp. Caller holds lock."""
        self._save_json(PATTERNS_FILE, self._patterns)
        self._pattern_ts = _now_utc().timestamp()

    def _save_explicit_unlocked(self) -> None:
        """Write explicit prefs to disk. Caller holds lock."""
        self._save_json(PREFS_FILE, self._explicit)

    def save_explicit(self) -> None:
        """Persist explicit prefs to disk (public, acquires lock)."""
        with self._lock:
            self._save_explicit_unlocked()

    def save_patterns(self) -> None:
        """Persist inferred patterns to disk (public, acquires lock)."""
        with self._lock:
            self._save_patterns_unlocked()

    # ── Explicit preference access ───────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Read a single preference, falling back to defaults."""
        if not self._loaded:
            self.load()
        return self._explicit.get(key, DEFAULT_PREFS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        """Set an explicit preference (validates type) and persist immediately."""
        self._validate_pref(key, value)
        with self._lock:
            if not self._loaded:
                self.load()
            self._explicit[key] = value
            self._save_explicit_unlocked()
            logger.info("Preference set: %s = %s", key, value)

    def _validate_pref(self, key: str, value: Any) -> None:
        """Raise TypeError if value doesn't match known schema for key.

        Unknown keys are allowed through with a warning.
        """
        expected = _PREF_SCHEMA.get(key)
        if expected is None:
            logger.info("Setting unvalidated preference key: %s", key)
            return

        if not isinstance(value, expected):
            raise TypeError(
                f"preference '{key}' expects {expected}, got {type(value).__name__}"
            )

        # Sub-validation: quiet_hours
        if key == "quiet_hours":
            if not isinstance(value, dict):
                raise TypeError("quiet_hours must be a dict with 'start' and 'end' keys")
            for sub_key in ("start", "end"):
                sub_val = value.get(sub_key, "")
                if not isinstance(sub_val, str):
                    raise TypeError(
                        f"quiet_hours.{sub_key} must be str, got {type(sub_val).__name__}"
                    )
                if not _validate_time_str(sub_val):
                    raise ValueError(
                        f"quiet_hours.{sub_key} must be HH:MM, got {sub_val!r}"
                    )

    def all_explicit(self) -> dict[str, Any]:
        """Return all explicit preferences (read-only copy)."""
        if not self._loaded:
            self.load()
        return dict(self._explicit)

    # ── Convenience queries ─────────────────────────────────────────────

    def is_quiet_hours(self) -> bool:
        """True if current local time is within quiet_hours window."""
        qh = self.get("quiet_hours", DEFAULT_PREFS["quiet_hours"])
        now = _now_local()
        current_min = now.hour * 60 + now.minute

        def _to_min(s: str) -> int:
            h, m = s.split(":")
            return int(h) * 60 + int(m)

        start = _to_min(qh.get("start", "22:00"))
        end   = _to_min(qh.get("end", "09:00"))

        if start <= end:
            return start <= current_min <= end
        # Overnight wrap: 22:00 → 09:00
        return current_min >= start or current_min <= end

    def briefing_order(self) -> list[str]:
        """Return ordered list of briefing sections per user prefs."""
        return self.get("briefing_priority", DEFAULT_PREFS["briefing_priority"])

    def alert_threshold(self, threshold_name: str, default: float) -> float:
        """Return a numeric alert threshold from prefs."""
        return float(self.get(threshold_name, default))

    # ── Inferred patterns ───────────────────────────────────────────────

    def pattern(self, key: str) -> Optional[dict[str, Any]]:
        """Return one inferred pattern dict (or None)."""
        if not self._loaded:
            self.load()
        return self._patterns.get(key)

    def all_patterns(self) -> dict[str, Any]:
        """Return all inferred patterns (read-only copy)."""
        if not self._loaded:
            self.load()
        return dict(self._patterns)

    def confidence_label(self, confidence: float) -> str:
        """Human label for a confidence score.

        Note: confidence is NOT floored — a pattern with CV=2.0 and n=3
        may score below 0.3 and return 'weak'. This is intentional.
        """
        if confidence >= CONFIDENCE_HIGH:
            return "high"
        if confidence >= CONFIDENCE_MODERATE:
            return "moderate"
        if confidence >= CONFIDENCE_LOW:
            return "low"
        return "weak"

    def maybe_refresh_patterns(self) -> bool:
        """Recompute inferred patterns if stale. Returns True if refreshed."""
        if not self.db_path:
            return False
        elapsed = _now_utc().timestamp() - self._pattern_ts
        if elapsed < REFRESH_INTERVAL:
            return False
        return self.refresh_patterns()

    def refresh_patterns(self) -> bool:
        """Run full pattern inference. Returns True on success.

        Uses internal unlock-aware save to avoid nested lock issues.
        """
        if not self.db_path:
            return False
        with self._lock:
            try:
                patterns = self._infer_patterns(self.db_path)
                self._patterns = patterns
                self._save_patterns_unlocked()
                logger.info("Patterns refreshed: %d inferred", len(patterns))
                return True
            except Exception as exc:
                logger.warning("Pattern inference failed: %s", exc)
                return False

    # ── Inference engine ────────────────────────────────────────────────

    def _infer_patterns(self, db_path: str) -> dict[str, Any]:
        """Run all pattern inferences against the Helios DB.

        Each pattern returned as::

            {
                "value": <inferred_value>,
                "confidence": 0.0–1.0,
                "samples": int,
                "source": "inferred",
                "last_updated": "ISO-8601",
            }
        """
        patterns: dict[str, Any] = {}
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        now_ts = _now_utc().isoformat()

        try:
            patterns["typical_sleep_hours"] = self._infer_stat(
                conn, "sleep.hours", "sleep")
            patterns["typical_activity_minutes"] = self._infer_stat(
                conn, "activity.minutes_daily", "activity")
            patterns["typical_resting_hr"] = self._infer_stat(
                conn, "resting_heart_rate.avg_daily", "health")
            patterns["typical_mood"] = self._infer_stat(
                conn, "mood.score_daily", "mood")
            patterns["weekday_focus_window"] = self._infer_focus_window(conn)
            patterns["typical_spotify_minutes"] = self._infer_stat(
                conn, "spotify.listen_minutes_daily", "spotify")
            patterns["gaming_tendency"] = self._infer_gaming_tendency(conn)
            patterns["low_sleep_correlates"] = self._infer_sleep_correlates(conn)
        finally:
            conn.close()

        for pat in patterns.values():
            if isinstance(pat, dict) and "last_updated" not in pat:
                pat["last_updated"] = now_ts

        return patterns

    def _infer_stat(
        self, conn: sqlite3.Connection, metric: str, label: str,
    ) -> dict[str, Any]:
        """Infer mean + stddev for a metric from 28 days of snapshots."""
        rows = conn.execute(
            "SELECT value FROM metric_snapshots "
            "WHERE metric=? AND date_key >= ? ORDER BY date_key",
            (metric, _days_ago(28)),
        ).fetchall()

        values = [r["value"] for r in rows if r["value"] is not None]
        n = len(values)

        if n < SAMPLES_MIN:
            return {"value": None, "confidence": 0.0, "samples": n,
                    "source": "inferred", "label": label}

        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        stddev  = variance ** 0.5

        # Confidence: higher with more samples + lower coefficient of variation.
        # NO floor — high-variance data stays weak (ChatGPT review blocker 9).
        cv = stddev / max(abs(mean), 0.001)
        raw_confidence = min(n / 14.0, 1.0) * (1.0 - min(cv, 1.0))
        confidence = min(raw_confidence, 1.0)

        return {
            "value": round(mean, 2),
            "stddev": round(stddev, 2),
            "confidence": round(confidence, 3),
            "samples": n,
            "source": "inferred",
            "label": label,
            "metric": metric,
        }

    def _infer_focus_window(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Find the most productive 4-hour window on weekdays."""
        rows = conn.execute(
            "SELECT ts, duration_secs FROM focus "
            "WHERE state='working' AND ts >= ? ORDER BY ts",
            (_days_ago(28),)
        ).fetchall()

        if len(rows) < SAMPLES_MIN:
            return {"value": None, "confidence": 0.0, "samples": len(rows),
                    "source": "inferred", "label": "focus_window"}

        hour_buckets: dict[int, float] = {}
        for r in rows:
            try:
                ts = r["ts"]
                if isinstance(ts, str):
                    h = int(ts[11:13])
                else:
                    h = ts.hour if hasattr(ts, "hour") else int(str(ts)[11:13])
                hour_buckets[h] = hour_buckets.get(h, 0) + (r["duration_secs"] or 0)
            except Exception:
                continue

        if not hour_buckets:
            return {"value": None, "confidence": 0.0, "samples": 0,
                    "source": "inferred", "label": "focus_window"}

        best_start = max(hour_buckets, key=hour_buckets.get)
        best_total = hour_buckets.get(best_start, 0)
        for start in range(6, 21):
            total = sum(hour_buckets.get(h, 0) for h in range(start, start + 4))
            if total > best_total:
                best_total = total
                best_start = start

        window_str = f"{best_start:02d}:00–{best_start + 4:02d}:00"
        total_hours = best_total / 3600.0
        confidence = min(len(hour_buckets) / 10.0, 1.0)

        return {
            "value": window_str,
            "total_hours": round(total_hours, 1),
            "confidence": round(confidence, 3),
            "samples": len(rows),
            "source": "inferred",
            "label": "focus_window",
        }

    def _infer_gaming_tendency(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Infer whether gaming happens late-night regularly.

        Uses proper local-time conversion (ChatGPT review blocker 5).
        Late-night = local hour >= 22 or < 4 (10 PM → 4 AM Mountain).
        """
        rows = conn.execute(
            "SELECT ts, duration_secs FROM focus "
            "WHERE state='gaming' AND ts >= ? ORDER BY ts",
            (_days_ago(28),)
        ).fetchall()

        if len(rows) < SAMPLES_MIN:
            return {"value": None, "confidence": 0.0, "samples": len(rows),
                    "source": "inferred", "label": "gaming"}

        late_count = 0
        total_secs = 0.0
        for r in rows:
            ts = r["ts"]
            if isinstance(ts, str):
                h = _local_hour(ts)
            else:
                h = int(str(ts)[11:13])
                if h >= 4:  # fallback — crude UTC check (kept as belt-and-suspenders)
                    pass
                h = _local_hour(str(ts))

            if h >= 22 or h < 4:  # 10 PM – 4 AM local
                late_count += 1
            total_secs += r["duration_secs"] or 0

        ratio = late_count / max(len(rows), 1)
        avg_daily_minutes = total_secs / max(len(rows), 1) / 60.0
        is_late = ratio > 0.4
        confidence = min(len(rows) / 14.0, 1.0)

        return {
            "value": {
                "late_night_ratio": round(ratio, 2),
                "late_night_tendency": is_late,
                "avg_daily_minutes": round(avg_daily_minutes, 0),
            },
            "confidence": round(confidence, 3),
            "samples": len(rows),
            "source": "inferred",
            "label": "gaming",
        }

    def _infer_sleep_correlates(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Check correlations table for sleep-linked metrics."""
        rows = conn.execute(
            "SELECT metric_a, metric_b, pearson_r, strength, direction "
            "FROM correlations "
            "WHERE (metric_a LIKE '%sleep%' OR metric_b LIKE '%sleep%') "
            "AND strength IN ('strong', 'moderate') "
            "ORDER BY ABS(pearson_r) DESC LIMIT 5"
        ).fetchall()

        if not rows:
            return {"value": None, "confidence": 0.0, "samples": 0,
                    "source": "inferred", "label": "sleep_correlates"}

        correlates = []
        best_r = 0.0
        for r in rows:
            correlates.append({
                "pair": [r["metric_a"], r["metric_b"]],
                "r": round(r["pearson_r"], 3),
                "strength": r["strength"],
                "direction": r["direction"],
            })
            best_r = max(best_r, abs(r["pearson_r"]))

        return {
            "value": correlates,
            "best_r": round(best_r, 3),
            "confidence": round(min(best_r, 1.0), 3),
            "samples": len(rows),
            "source": "inferred",
            "label": "sleep_correlates",
        }


# ── Module-level convenience ────────────────────────────────────────────────

_prefs_instance: Optional[PreferenceEngine] = None


def get_preferences(db_path: Optional[str] = None) -> PreferenceEngine:
    """Return the module-level PreferenceEngine singleton."""
    global _prefs_instance
    if _prefs_instance is None or (db_path and _prefs_instance.db_path != db_path):
        _prefs_instance = PreferenceEngine(db_path)
        _prefs_instance.load()
    return _prefs_instance


def reset_preferences() -> None:
    """Reset singleton (for testing)."""
    global _prefs_instance
    _prefs_instance = None
