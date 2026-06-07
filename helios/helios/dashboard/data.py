"""Read-only data loaders for the Helios dashboard.

All file reads are graceful — missing files, malformed JSON, and permission
errors produce empty/placeholder results rather than exceptions.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .privacy import (
    sanitize_dict,
    sanitize_health,
    sanitize_location,
    PUBLIC_SAFE,
)

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

HELIOS_HOME = Path(os.environ.get("HELIOS_HOME", os.path.expanduser("~/.hermes/helios")))
DATA_DIR = HELIOS_HOME / "data"

# ── Helpers ────────────────────────────────────────────────────────────────────

def _age_secs(timestamp_str: str | None) -> float | None:
    """Compute age in seconds from an ISO timestamp string."""
    if not timestamp_str:
        return None
    try:
        ts = datetime.fromisoformat(timestamp_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError):
        return None


def load_json_safe(path: Path, default: Any = None) -> Any:
    """Load a JSON file gracefully. Returns default on any error."""
    if default is None:
        default = {}
    try:
        if not path.exists():
            return default
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, PermissionError, OSError) as exc:
        log.warning("Failed to load %s: %s", path, exc)
        return default


def load_channel_events(limit: int = 20) -> list[dict[str, Any]]:
    """Load recent channel events from channel_log.jsonl.
    
    Returns the most recent `limit` events, parsed and sanitized.
    Malformed lines are skipped.
    """
    jsonl_path = DATA_DIR / "channel_log.jsonl"
    if not jsonl_path.exists():
        return []

    events: list[dict[str, Any]] = []
    try:
        with open(jsonl_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (PermissionError, OSError) as exc:
        log.warning("Failed to read channel_log.jsonl: %s", exc)
        return []

    # Return most recent `limit` events
    events = events[-limit:]
    return [sanitize_dict(e) for e in events]


def _module_stale(info: dict[str, Any], default_threshold_secs: float = 3600) -> bool:
    """Return True when a module exceeds its stale threshold.

    ModuleHealthTracker can persist per-module overrides for slow-changing data
    such as HealthKit. The dashboard must respect those overrides; otherwise
    healthy daily/periodic modules look falsely stale.
    """
    freshness = info.get("freshness_secs")
    if freshness is None:
        return False
    threshold = default_threshold_secs
    override = info.get("_freshness_threshold_override")
    if isinstance(override, dict):
        threshold = override.get("stale", threshold)
    try:
        return float(freshness) > float(threshold)
    except (TypeError, ValueError):
        return False


def _load_module_health(status: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract module health from latest_status.json."""
    modules = status.get("modules", {})
    results = []
    for name, info in modules.items():
        entry: dict[str, Any] = {
            "name": name,
            "state": info.get("state", "unknown"),
            "freshness_secs": info.get("freshness_secs"),
            "confidence": info.get("confidence"),
            "consecutive_ok": info.get("consecutive_ok"),
            "consecutive_failures": info.get("consecutive_failures"),
        }
        # Sanitize last_error: keep message only, no stack traces or URLs
        last_err = info.get("last_error")
        if last_err:
            entry["last_error_summary"] = str(last_err)[:120]
        results.append(entry)
    return results


def _load_context_summary(context: dict[str, Any]) -> dict[str, Any]:
    """Build a sanitized context summary from context_export.json."""
    summary: dict[str, Any] = {}

    # Location — city only, no coordinates
    metrics = context.get("metrics", {})
    loc = metrics.get("location", {})
    if not loc:
        loc = context.get("location", {})
    summary["location"] = sanitize_location(loc)

    # Weather — safe summary only
    weather = metrics.get("weather", {})
    if weather:
        summary["weather"] = {
            "summary": weather.get("summary", "unknown"),
            "temperature": weather.get("temperature_label", "[REDACTED]"),
            "condition": weather.get("condition", "unknown"),
        }
    else:
        summary["weather"] = {"summary": "no data"}

    # Calendar — count and next event title only
    cal = context.get("calendar", {})
    if isinstance(cal, dict):
        events = cal.get("events", [])
        summary["calendar"] = {
            "count": len(events) if isinstance(events, list) else 0,
            "next_event_title": (
                events[0].get("title", "[REDACTED]") if isinstance(events, list) and events else None
            ),
        }
    else:
        summary["calendar"] = {"count": 0}

    # Reminders count
    reminders = metrics.get("reminders", {})
    if isinstance(reminders, dict):
        summary["reminders_count"] = reminders.get("count", len(reminders) if isinstance(reminders, list) else 0)
    else:
        summary["reminders_count"] = 0

    # Focus/Idle summary
    focus = context.get("focus", {})
    if focus:
        summary["focus"] = {
            "state": focus.get("state", "unknown"),
            "app": focus.get("active_app", {}).get("name", "unknown") if isinstance(focus.get("active_app"), dict) else "unknown",
        }

    # Spotify — current track only (considered safe for local dashboard)
    spotify = metrics.get("spotify", {})
    if spotify and isinstance(spotify, dict):
        summary["spotify"] = {
            "track": spotify.get("track_name", "unknown"),
            "artist": spotify.get("artist", "unknown"),
            "is_playing": spotify.get("is_playing", False),
        }

    # Health — summarized, no raw readings
    health = context.get("health", {})
    if health:
        summary["health"] = sanitize_health(health)

    # Mood summary
    mood = context.get("mood", {})
    if mood and isinstance(mood, dict):
        latest_date = max(mood.keys()) if mood else None
        if latest_date:
            entry = mood[latest_date]
            if isinstance(entry, dict):
                summary["mood"] = {
                    "date": latest_date,
                    "score": entry.get("score"),
                    "label": entry.get("label", "unknown"),
                }

    return summary


def build_dashboard_snapshot() -> dict[str, Any]:
    """Build the full dashboard data snapshot from local files.
    
    Returns a dict with these top-level sections:
        - runtime_status
        - module_health
        - recent_events
        - context_summary
        - alerts
        - privacy_panel
    """
    snapshot: dict[str, Any] = {}

    # ── 1. Runtime Status ──────────────────────────────────────────────────
    status = load_json_safe(HELIOS_HOME / "latest_status.json")
    runtime: dict[str, Any] = {
        "engine": status.get("engine", "helios"),
        "version": status.get("version", "unknown"),
        "generated_at": status.get("generated_at"),
        "health": status.get("health", "unknown"),
        "last_tick_at": status.get("last_tick_at"),
        "tick_age_secs": _age_secs(status.get("last_tick_at")),
    }

    # Channel router status from status (if present)
    channels = status.get("channel_router", {})
    if channels:
        runtime["channel_router"] = sanitize_dict(channels)

    snapshot["runtime_status"] = runtime

    # ── 2. Module Health ───────────────────────────────────────────────────
    snapshot["module_health"] = _load_module_health(status)

    # ── 3. Recent Events ──────────────────────────────────────────────────
    snapshot["recent_events"] = load_channel_events(limit=20)

    # ── 4. Context Summary ────────────────────────────────────────────────
    context = load_json_safe(HELIOS_HOME / "context_export.json")
    snapshot["context_summary"] = _load_context_summary(context)

    # ── 5. Alerts & Warnings ───────────────────────────────────────────────
    alerts_data = load_json_safe(HELIOS_HOME / "alerts_recent.json")
    alert_list = alerts_data.get("alerts", [])
    open_alerts = status.get("open_alerts", [])

    snapshot["alerts"] = {
        "recent_count": len(alert_list) if isinstance(alert_list, list) else 0,
        "open_count": len(open_alerts) if isinstance(open_alerts, list) else 0,
        "recent": [sanitize_dict(a) for a in alert_list[:10]] if isinstance(alert_list, list) else [],
        "open": [sanitize_dict(a) for a in open_alerts[:10]] if isinstance(open_alerts, list) else [],
    }

    # Stale data warnings
    stale_modules = [
        m["name"] for m in snapshot["module_health"]
        if _module_stale(status.get("modules", {}).get(m["name"], {}))
    ]
    failed_modules = [
        m["name"] for m in snapshot["module_health"]
        if m.get("state") == "failed"
    ]
    if stale_modules:
        snapshot["alerts"]["stale_modules"] = stale_modules
    if failed_modules:
        snapshot["alerts"]["failed_modules"] = failed_modules

    # ── 6. Product Cards ──────────────────────────────────────────────────
    snapshot["cards"] = {
        "priority_engine": load_priority_engine_card(),
        "work_hours": load_work_hours_card(),
        "health_diary": load_health_diary_card(),
        "location_freshness": load_location_freshness_card(),
        "spotify": load_spotify_card(),
        "agenda": load_agenda_card(),
        "module_staleness": load_module_staleness_card(),
    }

    # ── 7. Privacy Panel ──────────────────────────────────────────────────
    from .privacy import privacy_panel
    snapshot["privacy_panel"] = privacy_panel()

    return snapshot


# ── Product Card Loaders ──────────────────────────────────────────────────

def load_priority_engine_card() -> dict[str, Any]:
    """Load priority-engine status without exposing raw payloads.

    Returns a compact, dashboard-ready summary from the latest JSON export
    and aggregate SQLite counts. This is visibility only: no dispatcher control
    and no raw candidate payloads are surfaced.
    """
    latest = load_json_safe(DATA_DIR / "priority_engine" / "latest.json")
    card: dict[str, Any] = {
        "mode": latest.get("mode", "unknown") if isinstance(latest, dict) else "unknown",
        "generated": latest.get("generated", 0) if isinstance(latest, dict) else 0,
        "scored": latest.get("scored", 0) if isinstance(latest, dict) else 0,
        "selected": latest.get("selected", 0) if isinstance(latest, dict) else 0,
        "suppressed": latest.get("suppressed", 0) if isinstance(latest, dict) else 0,
        "deferred": latest.get("deferred", 0) if isinstance(latest, dict) else 0,
        "top_candidates": [],
        "history": {
            "total_candidates": 0,
            "total_decisions": 0,
            "first_seen": None,
            "last_seen": None,
            "decisions": [],
        },
        "assessment": "No priority-engine export has been written yet.",
    }

    if isinstance(latest, dict):
        candidates = latest.get("top_candidates", [])
        if isinstance(candidates, list):
            card["top_candidates"] = [
                sanitize_dict({
                    "title": item.get("title"),
                    "type": item.get("type"),
                    "category": item.get("category"),
                    "score": item.get("score"),
                    "decision": item.get("decision"),
                    "route": item.get("route"),
                    "reason": item.get("reason"),
                })
                for item in candidates[:5]
                if isinstance(item, dict)
            ]

    db_path = HELIOS_HOME / "helios_v6.db"
    if db_path.exists():
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                counts = conn.execute(
                    """SELECT COUNT(*) AS total_candidates,
                              MIN(created_at) AS first_seen,
                              MAX(created_at) AS last_seen
                       FROM priority_candidates"""
                ).fetchone()
                decisions_total = conn.execute(
                    "SELECT COUNT(*) AS total_decisions FROM priority_decisions"
                ).fetchone()
                breakdown = conn.execute(
                    """SELECT mode, decision, route, COUNT(*) AS count,
                              ROUND(AVG(final_score), 3) AS avg_score
                       FROM priority_decisions
                       GROUP BY mode, decision, route
                       ORDER BY count DESC
                       LIMIT 6"""
                ).fetchall()

            card["history"] = {
                "total_candidates": counts["total_candidates"] if counts else 0,
                "total_decisions": decisions_total["total_decisions"] if decisions_total else 0,
                "first_seen": counts["first_seen"] if counts else None,
                "last_seen": counts["last_seen"] if counts else None,
                "decisions": [sanitize_dict(dict(row)) for row in breakdown],
            }
        except (sqlite3.Error, OSError) as exc:
            card["history_error"] = str(exc)[:120]

    mode = card.get("mode", "unknown")
    total = card.get("history", {}).get("total_candidates", 0)
    if mode == "shadow" and total:
        card["assessment"] = "Ranking is running, but it is still shadow-only; it observes and logs rather than improving delivery policy."
    elif mode not in ("unknown", "shadow") and total:
        card["assessment"] = "Priority engine is active beyond shadow mode. Verify dispatch outcomes before raising automation authority."
    elif total:
        card["assessment"] = "Priority history exists, but current export mode is unknown."

    return sanitize_dict(card)


def load_work_hours_card() -> dict[str, Any]:
    """Load work hours state and return a card-ready dict.

    Returns dict with:
        current_pay_period, copy_paste_timesheet, needs_review,
        confidence_counts, last_generated
    """
    state = load_json_safe(DATA_DIR / "work_hours_state.json")
    if not state:
        return {
            "current_pay_period": None,
            "copy_paste_timesheet": None,
            "needs_review": [],
            "confidence_counts": {},
            "last_generated": None,
        }

    days = state.get("days", [])
    needs_review = [
        {"date": d.get("date"), "reason": d.get("note", "")}
        for d in days
        if d.get("kind") == "needs_review"
    ]

    confidence_counts: dict[str, int] = {}
    for d in days:
        c = d.get("confidence", "unknown")
        confidence_counts[c] = confidence_counts.get(c, 0) + 1

    return sanitize_dict({
        "current_pay_period": state.get("period_label", state.get("period_start", "")),
        "copy_paste_timesheet": state.get("report_text"),
        "needs_review": needs_review,
        "confidence_counts": confidence_counts,
        "last_generated": state.get("generated_at"),
    })


def load_health_diary_card(date_str: str | None = None) -> dict[str, Any]:
    """Load health diary report for a date and return a card-ready dict.

    Returns dict with:
        sleep_hours, steps, active_minutes, mood_score (if available),
        stale_data_warnings, confidence
    """
    from datetime import date as _date
    if date_str is None:
        date_str = _date.today().isoformat()

    # Look for report file: health_diary_YYYY-MM-DD.json
    report_path = DATA_DIR / "reports" / f"health_diary_{date_str}.json"
    report = load_json_safe(report_path)

    if not report:
        # Also try without reports/ subdirectory
        report_path = HELIOS_HOME / f"health_diary_{date_str}.json"
        report = load_json_safe(report_path)

    if not report:
        return {
            "date": date_str,
            "sleep_hours": None,
            "steps": None,
            "active_minutes": None,
            "mood_score": None,
            "stale_data_warnings": [],
            "confidence": "needs_review",
        }

    items = report.get("items", [])
    metrics_map: dict[str, Any] = {}
    for item in items:
        key = item.get("key", "")
        val = item.get("value")
        if val is not None:
            metrics_map[key] = val

    stale_warnings = report.get("gaps", [])

    return sanitize_dict({
        "date": date_str,
        "sleep_hours": metrics_map.get("sleep_hours"),
        "steps": metrics_map.get("steps"),
        "active_minutes": metrics_map.get("active_minutes"),
        "mood_score": metrics_map.get("mood_score"),
        "stale_data_warnings": stale_warnings,
        "confidence": report.get("confidence", "needs_review"),
    })


def load_location_freshness_card() -> dict[str, Any]:
    """Load location module data and return a card-ready dict.

    Returns dict with:
        zone_label (coarse), last_updated, freshness_secs, confidence
        NO raw lat/lon
    """
    # Try icloud_location_sync.json first
    loc_data = load_json_safe(DATA_DIR / "icloud_location_sync.json")

    if not loc_data:
        # Try context_export.json as fallback
        context = load_json_safe(HELIOS_HOME / "context_export.json")
        loc_data = context.get("metrics", {}).get("location", {}) if context else {}

    if not loc_data:
        return {
            "zone_label": "unknown",
            "last_updated": None,
            "freshness_secs": None,
            "confidence": "needs_review",
        }

    # Build zone_label from city/region but never expose raw coords
    city = loc_data.get("city", "")
    source = loc_data.get("source", "unknown")
    zone = loc_data.get("zone", "")
    is_home = loc_data.get("is_home", False)

    if zone and zone != "away":
        zone_label = zone
    elif is_home:
        zone_label = "home"
    elif city:
        zone_label = city
    else:
        zone_label = source

    # Determine freshness_secs
    freshness_secs = loc_data.get("freshness_secs")
    last_updated = loc_data.get("last_updated") or loc_data.get("ts")

    if freshness_secs is None and last_updated:
        freshness_secs = _age_secs(last_updated)

    # Determine confidence
    confidence = "low"
    if freshness_secs is not None:
        if freshness_secs < 300:  # < 5 min
            confidence = "high"
        elif freshness_secs < 3600:  # < 1 hr
            confidence = "medium"

    return sanitize_dict({
        "zone_label": zone_label,
        "last_updated": last_updated,
        "freshness_secs": freshness_secs,
        "confidence": confidence,
    })


def load_spotify_card(date_str: str | None = None) -> dict[str, Any]:
    """Load spotify daily summary and return a card-ready dict.

    Returns dict with:
        total_minutes, top_artist (single, not list), session_count,
        late_night flag, confidence
    """
    from datetime import date as _date
    if date_str is None:
        date_str = _date.today().isoformat()

    # Look for spotify_daily_YYYY-MM-DD.json
    report_path = DATA_DIR / "reports" / f"spotify_daily_{date_str}.json"
    report = load_json_safe(report_path)

    if not report:
        return {
            "date": date_str,
            "total_minutes": None,
            "top_artist": None,
            "session_count": 0,
            "night_session": False,
            "confidence": "needs_review",
        }

    items = report.get("items", [])
    metrics_map: dict[str, Any] = {}
    for item in items:
        key = item.get("key", "")
        val = item.get("value")
        if val is not None:
            metrics_map[key] = val

    # Get top_artist — single artist, not a list
    top_artists = metrics_map.get("top_artists", [])
    top_artist = top_artists[0] if isinstance(top_artists, list) and top_artists else None

    # Late-night flag: derived from late_night_minutes > 0 or items
    late_night_minutes = metrics_map.get("late_night_minutes", 0)
    late_night = bool(late_night_minutes and late_night_minutes > 0) if late_night_minutes else False

    return sanitize_dict({
        "date": date_str,
        "total_minutes": metrics_map.get("total_minutes"),
        "top_artist": top_artist,
        "session_count": metrics_map.get("session_count", 0),
        "night_session": late_night,  # renamed from late_night to avoid false lat/lon match in sanitize_dict
        "confidence": report.get("confidence", "needs_review"),
    })


def load_agenda_card() -> dict[str, Any]:
    """Load agenda from calendar/tasks/reminders and return a card-ready dict.

    Returns dict with:
        events_count, next_event_title (sanitized), next_event_time,
        free_minutes_best (longest free block), overdue_count
    """
    # Load calendar events from context
    context = load_json_safe(HELIOS_HOME / "context_export.json")
    calendar = context.get("calendar", {}) if context else {}
    metrics = context.get("metrics", {}) if context else {}
    reminders_data = metrics.get("reminders", {}) if metrics else {}

    # Extract calendar events
    events_list = calendar.get("events", []) if isinstance(calendar, dict) else []
    if not isinstance(events_list, list):
        events_list = []

    # Count events
    events_count = len(events_list)

    # Next event title and time (sanitized)
    next_event_title = None
    next_event_time = None
    if events_list and isinstance(events_list[0], dict):
        raw_title = events_list[0].get("title", "")
        # Sanitize title — remove sensitive patterns
        from .privacy import _redact_string
        next_event_title = _redact_string(str(raw_title)[:200]) if raw_title else None
        next_event_time = events_list[0].get("start") or events_list[0].get("start_time")

    # Longest free block — estimate from events count
    # (Precise calculation requires AgendaItem; use context estimate if available)
    free_minutes_best = calendar.get("free_block_minutes")
    if free_minutes_best is None:
        # Rough heuristic: if < 2 events, assume substantial free time
        free_minutes_best = 480 if events_count < 2 else None

    # Overdue count from reminders
    overdue_count = reminders_data.get("overdue", 0) if isinstance(reminders_data, dict) else 0

    return sanitize_dict({
        "events_count": events_count,
        "next_event_title": next_event_title,
        "next_event_time": next_event_time,
        "free_minutes_best": free_minutes_best,
        "overdue_count": overdue_count,
    })


def load_module_staleness_card(
    threshold_secs: float = 3600,
) -> dict[str, Any]:
    """Load module health data and return a staleness card.

    Returns dict with:
        modules: list of {module_name, freshness_secs, confidence, state}
        stale_modules: list of module names with freshness > threshold
        summary: human-readable text
    """
    status = load_json_safe(HELIOS_HOME / "latest_status.json")
    modules_raw = status.get("modules", {}) if status else {}

    modules: list[dict[str, Any]] = []
    stale_modules: list[str] = []

    for name, info in modules_raw.items():
        if not isinstance(info, dict):
            continue
        freshness = info.get("freshness_secs")
        entry = {
            "module_name": name,
            "freshness_secs": freshness,
            "confidence": info.get("confidence"),
            "state": info.get("state", "unknown"),
        }
        modules.append(entry)
        if _module_stale(info, threshold_secs):
            stale_modules.append(name)

    if not modules:
        summary = "No module health data available."
    elif stale_modules:
        summary = f"{len(stale_modules)} module(s) stale: {', '.join(stale_modules)}"
    else:
        summary = "All modules fresh."

    return sanitize_dict({
        "modules": modules,
        "stale_modules": stale_modules,
        "summary": summary,
    })