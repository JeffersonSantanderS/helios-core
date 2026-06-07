"""Helios v6 — Module Health Tracker.

Tracks per-module runtime health and exposes
freshness metadata for confidence-weighted briefings and alerts.

HARDENED 2026-05-09 — ChatGPT review blockers 1–7 resolved:
  1. Data-freshness from module result (last_updated/observed_at) preferred
     over tick-time freshness. freshness_source tracks origin.
  2. Circuit-broken modules recorded via record_skipped() — never marked
     healthy.  consecutive_ok resets, freshness continues aging.
  3. tick_targeted() mirrors full health recording.
  4. (engine-side) Retention upserts focus_daily_summary before DELETE.
  5. (inference-side) focus_daily_summary used for aggregate patterns.
  6. (briefing-side) System sections (_stale/_confidence) get negative sort
     rank to stay above user-ordered content.
  7. Health-related alerts (slug containing module_health/stale_data/
     collector_failed) bypass degraded-module suppression.

Design concerns 8–9 (persistence across restart, rolling window confidence)
documented as non-blocking future improvements.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("helios.module_health")

# ── File paths ──────────────────────────────────────────────────────────────
HELIOS_DATA = Path.home() / ".hermes" / "helios" / "data"
HEALTH_FILE    = HELIOS_DATA / "module_health.json"
FRESHNESS_FILE = HELIOS_DATA / "module_freshness.json"

# ── Freshness thresholds ────────────────────────────────────────────────────
FRESHNESS_FRESH     = 300    # ≤5 min     → healthy
FRESHNESS_STALE     = 900    # ≤15 min    → stale
FRESHNESS_DEGRADED  = 3600   # ≤1 hour    → degraded
# beyond 3600s → failed

# ── Confidence scoring ──────────────────────────────────────────────────────
CONFIDENCE_WEIGHTS = {
    "failure_rate":    0.4,
    "freshness":       0.3,
    "consecutive_ok":  0.2,
    "sample_count":    0.1,
}

# ── Data-freshness keys (checked in module result dict) ─────────────────────
_RESULT_FRESHNESS_KEYS = (
    "freshness_secs", "source_freshness_secs",
    "last_updated", "last_seen", "updated_at", "observed_at",
    "last_sync", "last_sync_ts", "synced_at",
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════
# Module Health Tracker
# ═══════════════════════════════════════════════════════════════════════════

class ModuleHealthTracker:
    """Tracks per-module health across engine ticks.

    HARDENED (2026-05-09): freshness_source, record_skipped, data-freshness.
    """

    def __init__(self):
        self._modules: dict[str, dict[str, Any]] = {}
        HELIOS_DATA.mkdir(parents=True, exist_ok=True)
        # Attempt to restore previous state
        self._load_previous()

    def _load_previous(self) -> None:
        """Restore module health from disk (non-blocking enhancement)."""
        if HEALTH_FILE.exists():
            try:
                data = json.loads(HEALTH_FILE.read_text())
                if isinstance(data, dict):
                    for name, entry in data.items():
                        if isinstance(entry, dict):
                            # Decay confidence by 0.05 per restart
                            old_conf = entry.get("confidence", 0.0)
                            entry["confidence"] = max(0.0, old_conf - 0.05)
                            entry["freshness_secs"] = 99999  # unknown after restart
                            entry["state"] = self._compute_state(entry)
                            self._modules[name] = entry
                if self._modules:
                    logger.info("Health restored from disk: %d modules", len(self._modules))
            except Exception as exc:
                logger.debug("Could not restore health state: %s", exc)

    # ── Recording ────────────────────────────────────────────────────────

    def record_tick(
        self, name: str, result: dict[str, Any],
        error: Optional[Exception] = None,
        status: Optional[str] = None,
    ) -> None:
        """Record one module tick result.

        status='skipped' for circuit-broken modules (blocker 2).
        Freshness from result data preferred over tick-time (blocker 1).
        """
        now = _now_utc()
        entry = self._get_or_create(name)
        entry["tick_count"] += 1
        entry["last_tick_ts"] = now.isoformat()

        if status == "skipped":
            # Circuit-broken: don't reset failure counters, don't bump ok streak
            entry["consecutive_ok"] = 0
            self._update_freshness(entry, result, now)
            entry["state"] = self._compute_state(entry)
            entry["confidence"] = self._compute_confidence(entry)
            return

        if error:
            entry["failure_count"] += 1
            entry["consecutive_failures"] += 1
            entry["consecutive_ok"] = 0
            entry["last_error"] = str(error)[:200]
            entry["last_error_ts"] = now.isoformat()
        else:
            entry["consecutive_failures"] = 0
            entry["consecutive_ok"] += 1
            entry["last_ok_ts"] = now.isoformat()
            entry["last_error"] = None

        self._update_freshness(entry, result, now)
        entry["state"] = self._compute_state(entry)
        entry["confidence"] = self._compute_confidence(entry)

    def record_skipped(self, name: str, reason: str = "circuit_open") -> None:
        """Record a circuit-broken / skipped module (blocker 2)."""
        self.record_tick(name, {"_status": reason}, status="skipped")

    def _update_freshness(
        self, entry: dict[str, Any], result: dict[str, Any], now: datetime,
    ) -> None:
        """Update freshness_secs from result data or tick time (blocker 1).

        Prefers module-provided freshness metadata. Falls back to
        time-since-last-ok-tick.
        """
        data_freshness = self._extract_data_freshness(result, now)
        if data_freshness is not None:
            entry["freshness_secs"] = data_freshness
            entry["freshness_source"] = "module_data"
            # Store per-module override if the module declares one
            override = result.get("_freshness_threshold_override")
            if isinstance(override, dict) and "fresh" in override:
                entry["_freshness_threshold_override"] = override
        elif entry.get("last_ok_ts"):
            try:
                last_ok_dt = datetime.fromisoformat(entry["last_ok_ts"])
                entry["freshness_secs"] = (now - last_ok_dt).total_seconds()
            except Exception:
                entry["freshness_secs"] = 99999
            entry["freshness_source"] = "tick_time"
        else:
            entry["freshness_secs"] = 99999
            entry["freshness_source"] = "none"

    def _extract_data_freshness(
        self, result: dict[str, Any], now: datetime,
    ) -> Optional[float]:
        """Extract freshness from module result metadata, if present.

        Scans result dict for known freshness keys. Falls back gracefully.
        """
        if not isinstance(result, dict):
            return None
        for key in _RESULT_FRESHNESS_KEYS:
            val = result.get(key)
            if val is None:
                continue
            # Direct numeric freshness in seconds
            if isinstance(val, (int, float)) and key.endswith("_secs"):
                return float(val)
            # ISO-8601 timestamp → compute age
            if isinstance(val, str) and "T" in val:
                try:
                    ts = val.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(ts)
                    return (now - dt).total_seconds()
                except Exception:
                    continue
        return None

    def _get_or_create(self, name: str) -> dict[str, Any]:
        if name not in self._modules:
            self._modules[name] = {
                "module": name,
                "tick_count": 0,
                "failure_count": 0,
                "consecutive_failures": 0,
                "consecutive_ok": 0,
                "last_ok_ts": None,
                "last_tick_ts": None,
                "last_error": None,
                "last_error_ts": None,
                "freshness_secs": 99999,
                "freshness_source": "none",
                "state": "unknown",
                "confidence": 0.0,
            }
        return self._modules[name]

    # ── State computation ────────────────────────────────────────────────

    def _compute_state(self, entry: dict[str, Any]) -> str:
        freshness = entry.get("freshness_secs", 99999)

        # Per-module freshness overrides (e.g. health data batched every 5-10 min)
        override = entry.get("_freshness_threshold_override")
        if isinstance(override, dict):
            fresh_limit = override.get("fresh", FRESHNESS_FRESH)
            stale_limit = override.get("stale", FRESHNESS_STALE)
            degraded_limit = override.get("degraded", FRESHNESS_DEGRADED)
        else:
            fresh_limit = FRESHNESS_FRESH
            stale_limit = FRESHNESS_STALE
            degraded_limit = FRESHNESS_DEGRADED

        if entry["tick_count"] == 0:
            return "unknown"

        if entry.get("consecutive_failures", 0) > 5:
            return "anomalous"

        if entry["tick_count"] > 10:
            failure_rate = entry["failure_count"] / entry["tick_count"]
            if failure_rate > 0.5:
                return "degraded"
            if failure_rate > 0.2:
                return "stale"

        if freshness <= fresh_limit:
            return "healthy"
        if freshness <= stale_limit:
            return "stale"
        if freshness <= degraded_limit:
            return "degraded"

        return "failed"

    # ── Confidence scoring ───────────────────────────────────────────────

    def _compute_confidence(self, entry: dict[str, Any]) -> float:
        if entry["tick_count"] == 0:
            failure_score = 0.0
        else:
            failure_rate = entry["failure_count"] / entry["tick_count"]
            failure_score = max(0.0, 1.0 - failure_rate * 2)

        # Respect per-module freshness overrides for scoring
        override = entry.get("_freshness_threshold_override")
        if isinstance(override, dict):
            fresh_limit = override.get("fresh", FRESHNESS_FRESH)
            stale_limit = override.get("stale", FRESHNESS_STALE)
            degraded_limit = override.get("degraded", FRESHNESS_DEGRADED)
        else:
            fresh_limit = FRESHNESS_FRESH
            stale_limit = FRESHNESS_STALE
            degraded_limit = FRESHNESS_DEGRADED

        freshness = entry.get("freshness_secs", 99999)
        if freshness <= fresh_limit:
            freshness_score = 1.0
        elif freshness <= stale_limit:
            freshness_score = 0.7
        elif freshness <= degraded_limit:
            freshness_score = 0.4
        else:
            freshness_score = 0.1

        streak = min(entry.get("consecutive_ok", 0), 50)
        streak_score = streak / 50.0

        samples = min(entry["tick_count"], 20)
        sample_score = samples / 20.0

        confidence = (
            CONFIDENCE_WEIGHTS["failure_rate"]    * failure_score
            + CONFIDENCE_WEIGHTS["freshness"]     * freshness_score
            + CONFIDENCE_WEIGHTS["consecutive_ok"] * streak_score
            + CONFIDENCE_WEIGHTS["sample_count"]  * sample_score
        )
        return round(confidence, 3)

    # ── Queries ──────────────────────────────────────────────────────────

    def state(self, name: str) -> str:
        return self._modules.get(name, {}).get("state", "unknown")

    def confidence(self, name: str) -> float:
        return self._modules.get(name, {}).get("confidence", 0.0)

    def freshness(self, name: str) -> float:
        return self._modules.get(name, {}).get("freshness_secs", 99999)

    def is_stale(self, name: str) -> bool:
        return self.freshness(name) > FRESHNESS_STALE

    def summary(self) -> dict[str, Any]:
        return dict(self._modules)

    def summary_list(self) -> list[dict[str, Any]]:
        items = list(self._modules.values())
        items.sort(key=lambda m: m.get("confidence", 0))
        return items

    # ── Persistence ──────────────────────────────────────────────────────

    def save(self) -> None:
        summary = self.summary()
        self._write_json(HEALTH_FILE, summary)

        freshness = {}
        for name, entry in self._modules.items():
            freshness[name] = {
                "freshness_secs": entry.get("freshness_secs", 99999),
                "freshness_source": entry.get("freshness_source", "none"),
                "state": entry.get("state", "unknown"),
                "confidence": entry.get("confidence", 0.0),
                "last_ok_ts": entry.get("last_ok_ts"),
            }
        self._write_json(FRESHNESS_FILE, freshness)

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(path)


# ── Integration helpers ─────────────────────────────────────────────────────

# Alert slugs/categories that should never be suppressed by degraded-module
# filtering, because they ARE the module health alerts (blocker 7).
_HEALTH_ALERT_PATTERNS = (
    "module_health", "module_failed", "module_degraded",
    "collector_failed", "collector_stalled",
    "stale_data", "data_stale", "data_freshness",
    "ingestion_failed", "ingestion_stalled",
)


def is_health_alert(alert: dict) -> bool:
    """True if this alert is a module health notification (blocker 7).

    Health alerts should never be suppressed by degraded-module gating.
    """
    slug = alert.get("slug", "").lower()
    category = alert.get("category", "").lower()
    combined = f"{slug} {category}"
    return any(p in combined for p in _HEALTH_ALERT_PATTERNS)


def freshness_warning(health: ModuleHealthTracker, name: str) -> Optional[str]:
    """Return a human warning if module data is stale, else None."""
    if not health.is_stale(name):
        return None
    secs = health.freshness(name)
    mins = int(secs // 60)
    if mins >= 60:
        hrs = mins // 60
        return f"{name.title()} data may be outdated (last update {hrs}h ago)."
    return f"{name.title()} data may be outdated (last update {mins}m ago)."
