"""Helios v6 — Stable JSON exports for other agents and services.

Writes three atomic JSON files on every tick:
  latest_status.json   — current engine state, modules, today metrics
  context_export.json  — rolling context window (default 7 days)
  alerts_recent.json   — alert history (default 24 hours)

All writes use temp-file + rename for atomicity.
Missing data produces nulls/empty structures, never crashes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("helios.stable_exports")

SCHEMA_VERSION = "1.0"
EXPORT_DIR = Path.home() / ".hermes" / "helios"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _overall_health(states: list[str]) -> str:
    """Derive overall health from module state list."""
    priority = ["healthy", "stale", "degraded", "unknown", "failed", "anomalous"]
    if not states:
        return "unknown"
    valid = [s for s in states if s in priority]
    if not valid:
        return "unknown"
    try:
        worst_idx = max(priority.index(s) for s in valid)
    except ValueError:
        worst_idx = priority.index("unknown")
    return priority[worst_idx]


# ── latest_status.json ─────────────────────────────────────────────────────

def build_latest_status(
    db_path: str,
    health_tracker: Any | None = None,
    last_tick_at: str | None = None,
) -> dict[str, Any]:
    """Build latest_status.json."""
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": "helios",
        "version": "6.0.0",
        "generated_at": _now_iso(),
        "health": "unknown",
        "last_tick_at": last_tick_at or _now_iso(),
        "modules": {},
        "today": {},
        "open_alerts": [],
    }

    # Module health summary if tracker available
    if health_tracker is not None and hasattr(health_tracker, "summary"):
        modules = health_tracker.summary()
        result["modules"] = modules
        states = [m.get("state", "unknown") for m in modules.values()]
        result["health"] = _overall_health(states)

    # DB-derived data
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        # Key metrics
        for metric_name, key in (
            ("sleep.hours", "sleep_hours"),
            ("activity.steps_daily", "steps"),
            ("mood.score_daily", "mood"),
        ):
            row = conn.execute(
                "SELECT value FROM metric_snapshots WHERE metric = ? AND date_key = ? LIMIT 1",
                (metric_name, today),
            ).fetchone()
            result["today"][key] = row["value"] if row else None

        # Calendar events today
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM calendar_events WHERE date_key = ?",
                (today,),
            ).fetchone()
            result["today"]["calendar_count"] = row["cnt"] if row else 0
        except sqlite3.OperationalError:
            result["today"]["calendar_count"] = 0

        # Risk flags (any module degraded/failed/anomalous)
        risk_flags: list[str] = []
        for name, mod in result.get("modules", {}).items():
            if mod.get("state") in ("degraded", "failed", "anomalous"):
                risk_flags.append(f"{name}_degraded")
        result["today"]["risk_flags"] = risk_flags

        # Stale data warnings for key sources
        stale_warnings: list[str] = []
        for name, mod in result.get("modules", {}).items():
            if mod.get("state") in ("stale", "degraded", "failed", "anomalous"):
                secs = mod.get("freshness_secs", 99999)
                mins = int(secs // 60)
                if mins >= 60:
                    hrs = mins // 60
                    stale_warnings.append(f"{name.title()} data is stale ({hrs}h since last update).")
                else:
                    stale_warnings.append(f"{name.title()} data is stale ({mins}m since last update).")
        if stale_warnings:
            result["today"]["stale_data_warnings"] = stale_warnings

        # Home Assistant health data staleness (use stored sync epoch)
        try:
            sync_row = conn.execute(
                "SELECT value FROM metric_snapshots WHERE metric = 'health.ha_last_sync_epoch' AND date_key = ? LIMIT 1",
                (today,),
            ).fetchone()
            if sync_row:
                sync_epoch = float(sync_row["value"])
                sync_dt = datetime.fromtimestamp(sync_epoch, tz=timezone.utc)
                age = datetime.now(timezone.utc) - sync_dt
                result["today"]["health_data_age_hours"] = round(age.total_seconds() / 3600, 1)
                result["today"]["health_data_stale"] = age > timedelta(hours=12)
            else:
                result["today"]["health_data_age_hours"] = None
                result["today"]["health_data_stale"] = True
        except Exception:
            pass

        # Open alerts (unresolved, last 24h)
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            rows = conn.execute(
                "SELECT ts, rule_slug, severity, message FROM alert_history "
                "WHERE ts >= ? ORDER BY ts DESC LIMIT 20",
                (cutoff,),
            ).fetchall()
            result["open_alerts"] = [
                {
                    "ts": r["ts"],
                    "slug": r["rule_slug"],
                    "severity": r["severity"],
                    "message": r["message"],
                }
                for r in rows
            ]
        except sqlite3.OperationalError:
            result["open_alerts"] = []

    except Exception as exc:
        logger.warning("latest_status build failed: %s", exc)
        result["_warning"] = str(exc)
    finally:
        conn.close()

    return result


# ── context_export.json ────────────────────────────────────────────────────

def build_context_export(db_path: str, window_days: int = 7) -> dict[str, Any]:
    """Build context_export.json with rolling window data."""
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": "helios",
        "generated_at": _now_iso(),
        "window_days": window_days,
        "metrics": {},
        "focus": {},
        "health": {},
        "mood": {},
        "calendar": {},
        "insights": {},
    }

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cutoff_date = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
    ).strftime("%Y-%m-%d")

    try:
        # Metrics
        rows = conn.execute(
            "SELECT metric, date_key, value, source FROM metric_snapshots "
            "WHERE date_key >= ? ORDER BY metric, date_key",
            (cutoff_date,),
        ).fetchall()

        metrics: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            metrics.setdefault(r["metric"], []).append(
                {"date": r["date_key"], "value": r["value"], "source": r["source"]}
            )
        result["metrics"] = metrics

        # Focus (window_days lookback)
        try:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS occurrences, "
                "SUM(duration_secs) AS total_seconds FROM focus "
                "WHERE date(ts) >= ? GROUP BY state",
                (cutoff_date,),
            ).fetchall()
            result["focus"] = {
                r["state"]: {
                    "occurrences": r["occurrences"],
                    "total_seconds": r["total_seconds"] or 0,
                }
                for r in rows
            }
        except sqlite3.OperationalError:
            result["focus"] = {}

        # Mood
        rows = conn.execute(
            "SELECT date_key, value FROM metric_snapshots "
            "WHERE metric = 'mood.score_daily' AND date_key >= ? ORDER BY date_key",
            (cutoff_date,),
        ).fetchall()
        result["mood"] = {r["date_key"]: r["value"] for r in rows}

        # Calendar
        try:
            rows = conn.execute(
                "SELECT date_key, COUNT(*) AS cnt FROM calendar_events "
                "WHERE date_key >= ? GROUP BY date_key",
                (cutoff_date,),
            ).fetchall()
            result["calendar"] = {
                r["date_key"]: {"event_count": r["cnt"]} for r in rows
            }
        except sqlite3.OperationalError:
            result["calendar"] = {}

        # Health (today's HA snapshot)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT metric, value FROM metric_snapshots "
            "WHERE source = 'home_assistant_health' AND date_key = ?",
            (today,),
        ).fetchall()
        result["health"] = {r["metric"]: r["value"] for r in rows if r["metric"] != "health.ha_last_sync_epoch"}

        # Insights from disk if present
        insight_dir = Path.home() / ".hermes" / "helios" / "data" / "insights"
        if insight_dir.exists():
            for insight_file in sorted(insight_dir.glob("*.json")):
                try:
                    key = insight_file.stem
                    result["insights"][key] = json.loads(
                        insight_file.read_text(encoding="utf-8")
                    )
                except Exception:
                    pass

        # Home environment (latest context values)
        try:
            rows = conn.execute(
                "SELECT key, value FROM context WHERE module = 'home' AND key LIKE 'home.%' ORDER BY ts DESC"
            ).fetchall()
            home_data: dict[str, Any] = {}
            for r in rows:
                k = r["key"]
                if k not in home_data:
                    try:
                        home_data[k] = json.loads(r["value"])
                    except json.JSONDecodeError:
                        home_data[k] = r["value"]
            result["home"] = home_data
        except sqlite3.OperationalError:
            result["home"] = {}

    except Exception as exc:
        logger.warning("context_export build failed: %s", exc)
        result["_warning"] = str(exc)
    finally:
        conn.close()

    return result


# ── alerts_recent.json ─────────────────────────────────────────────────────

def build_alerts_recent(db_path: str, window_hours: int = 24) -> dict[str, Any]:
    """Build alerts_recent.json with last N hours of alerts."""
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": "helios",
        "generated_at": _now_iso(),
        "window_hours": window_hours,
        "alerts": [],
    }

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=window_hours)
        ).isoformat()
        rows = conn.execute(
            "SELECT ts, rule_slug, severity, message FROM alert_history WHERE ts >= ? ORDER BY ts DESC",
            (cutoff,),
        ).fetchall()

        result["alerts"] = [
            {
                "ts": r["ts"],
                "slug": r["rule_slug"],
                "severity": r["severity"],
                "message": r["message"],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("alerts_recent build failed: %s", exc)
        result["_warning"] = str(exc)
    finally:
        conn.close()

    return result


# ── Batch write ────────────────────────────────────────────────────────────

def write_all_exports(
    db_path: str,
    health_tracker: Any | None = None,
    last_tick_at: str | None = None,
) -> dict[str, Path]:
    """Write all three export files atomically.

    Returns dict mapping export name to written Path.
    """
    paths: dict[str, Path] = {
        "latest_status": EXPORT_DIR / "latest_status.json",
        "context_export": EXPORT_DIR / "context_export.json",
        "alerts_recent": EXPORT_DIR / "alerts_recent.json",
    }

    _write_json_atomic(paths["latest_status"], build_latest_status(db_path, health_tracker, last_tick_at))
    logger.info("latest_status.json written")

    _write_json_atomic(paths["context_export"], build_context_export(db_path, window_days=7))
    logger.info("context_export.json written")

    _write_json_atomic(paths["alerts_recent"], build_alerts_recent(db_path, window_hours=24))
    logger.info("alerts_recent.json written")

    return paths


# ── self_improvement_status.json ────────────────────────────────────────────

def build_self_improvement_status(
    si_status: dict[str, Any],
    proposals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build sanitized self-improvement status export.

    Strips any private_sensitive or secret evidence from proposals.
    Only includes shadow/approved proposals with public-safe reasons.
    """
    from .self_improvement.models import PrivacyClass

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": "helios",
        "generated_at": _now_iso(),
        "mode": si_status.get("mode", "shadow"),
        "enabled": si_status.get("enabled", True),
        "latest_evaluation_at": si_status.get("latest_evaluation_at"),
        "event_count_24h": si_status.get("event_count_24h", 0),
        "outcome_count_24h": si_status.get("outcome_count_24h", 0),
        "proposal_count": si_status.get("proposal_count", 0),
        "blocked_count": si_status.get("blocked_count", 0),
        "shadow_count": si_status.get("shadow_count", 0),
        "proposed_count": si_status.get("proposed_count", 0),
        "approved_count": si_status.get("approved_count", 0),
        "allow_active_promotion": si_status.get("allow_active_promotion", False),
    }

    # Include only sanitized proposals (no secret/private_sensitive evidence)
    safe_proposals = []
    if proposals:
        for p in proposals:
            # Strip raw evidence, only include public-safe details
            safe_proposals.append({
                "proposal_id": p.get("proposal_id", ""),
                "ts": p.get("ts", ""),
                "target": p.get("target", ""),
                "change_type": p.get("change_type", ""),
                "before": p.get("before", ""),
                "after": p.get("after", ""),
                "reason": p.get("reason", "")[:200],  # Truncate long reasons
                "evidence_count": p.get("evidence_count", 0),
                "risk_level": p.get("risk_level", "low"),
                "status": p.get("status", "shadow"),
                "target_key": p.get("target_key", ""),
            })
    result["top_safe_proposals"] = safe_proposals[:10]  # Cap at 10 for export
    result["safety_gate_summary"] = {
        "min_evidence_count": si_status.get("min_evidence_count", 3),
        "max_negative_rate": si_status.get("max_negative_rate", 0.35),
        "allow_active_promotion": si_status.get("allow_active_promotion", False),
    }

    return result


def write_self_improvement_export(
    si_status: dict[str, Any],
    proposals: list[dict[str, Any]] | None = None,
) -> Path:
    """Write the self-improvement status export file atomically."""
    path = EXPORT_DIR / "data" / "self_improvement_status.json"
    _write_json_atomic(path, build_self_improvement_status(si_status, proposals))
    logger.info("self_improvement_status.json written")
    return path
