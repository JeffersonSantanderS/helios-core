"""Context API sanitization layer.

Builds on helios.dashboard.privacy for the core redaction engine and adds
context-API-specific post-processing:

    1. Strip host-specific filesystem paths (e.g. /home/..., ~/.hermes/...).
    2. Remove any remaining Santander-specific or corporate identifiers.
    3. Enforce deterministic key ordering so the contract response is stable
       across runs and machines.

All data returned by the /api/v1/context endpoint MUST pass through
``sanitize_for_contract`` before leaving this service.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from helios.dashboard.privacy import (
    sanitize_dict as _dashboard_sanitize,
    sanitize_health,
    sanitize_location,
    NEVER_EXPORT_FIELDS,
    PRIVATE_FIELDS,
)

# ── Path patterns that must be redacted ──────────────────────────────────────

_HOME_RE = re.compile(
    r"(?:/home|/Users|C:\\Users)[\\/][\w.-]+"
    r"|~[/\\]"
    r"|/mnt/[c-z][/\\]Users[/\\][\w.-]+"
)
_HERMES_PATH_RE = re.compile(
    r"(?:/)?\.hermes[/\\][\w.-]+[/\\]?[\w./\\-]*",
    re.IGNORECASE,
)

# ── Corporate / infrastructure identifiers that must never leak ───────────────

_CORPORATE_BLOCKLIST: frozenset[str] = frozenset({
    "santander", "santander_group", "santander_network",
    "banco_santander", "santander_bank",
})

_CORPORATE_RE = re.compile(
    r"\bsantander\b",
    re.IGNORECASE,
)


def _redact_paths(value: str) -> str:
    """Replace filesystem / host paths with safe placeholders."""
    value = _HOME_RE.sub("[HOME]", value)
    value = _HERMES_PATH_RE.sub("[HERMES_PATH]", value)
    return value


def _redact_corporate(value: str) -> str:
    """Remove any Santander-specific identifiers from a string."""
    return _CORPORATE_RE.sub("[REDACTED]", value)


def sanitize_for_contract(data: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a data dict for the context API contract response.

    Pipeline:
        1. Apply dashboard-level privacy sanitization (never-export, private
           field redaction, coordinate stripping, string token redaction).
        2. Walk the resulting dict and redact any surviving host paths,
           hermes paths, and corporate identifiers.

    The result is deterministic: same logical input always yields the same
    output structure regardless of the host machine or user home directory.
    """
    # Step 1 — dashboard privacy layer
    result = _dashboard_sanitize(data)

    # Step 2 — additional context-API post-processing
    result = _post_process(result)

    return result


def _post_process(data: Any) -> Any:
    """Recursively apply path and corporate redaction to all string values."""
    if isinstance(data, dict):
        cleaned: dict[str, Any] = {}
        for key, value in data.items():
            # Also check keys for corporate identifiers
            safe_key = _redact_corporate(str(key))
            cleaned[safe_key] = _post_process(value)
        return cleaned
    elif isinstance(data, list):
        return [_post_process(item) for item in data]
    elif isinstance(data, str):
        result = _redact_paths(data)
        result = _redact_corporate(result)
        return result
    else:
        return data


def build_contract_context(
    context_export: dict[str, Any],
    latest_status: dict[str, Any],
) -> dict[str, Any]:
    """Build the deterministic contract response payload.

    Combines data from context_export.json and latest_status.json into a
    stable, privacy-safe structure suitable for the /api/v1/context endpoint.

    The response is organized into these top-level sections:
        - runtime: engine version, health label, tick timing
        - modules: per-module health summary (state, freshness, confidence)
        - location: city/zone only, no coordinates
        - weather: summary only
        - calendar: event count and next-event title (sanitized)
        - focus: state and app name only
        - health: sanitized health metrics
        - mood: latest score and label

    No private data, no host paths, no corporate identifiers.
    """
    raw: dict[str, Any] = {}

    # ── Runtime ────────────────────────────────────────────────────────
    raw["runtime"] = {
        "engine": latest_status.get("engine", "helios"),
        "version": latest_status.get("version", "unknown"),
        "health": latest_status.get("health", "unknown"),
        "last_tick_at": latest_status.get("last_tick_at"),
    }

    # ── Modules ────────────────────────────────────────────────────────
    modules_raw = latest_status.get("modules", {})
    modules: list[dict[str, Any]] = []
    for name, info in modules_raw.items():
        if not isinstance(info, dict):
            continue
        modules.append({
            "name": name,
            "state": info.get("state", "unknown"),
            "freshness_secs": info.get("freshness_secs"),
            "confidence": info.get("confidence"),
            "consecutive_ok": info.get("consecutive_ok"),
            "consecutive_failures": info.get("consecutive_failures"),
        })
    raw["modules"] = modules

    # ── Context-derived sections ───────────────────────────────────────
    metrics = context_export.get("metrics", {})

    # Location — city/zone only
    loc = metrics.get("location", {}) or context_export.get("location", {})
    raw["location"] = sanitize_location(loc)

    # Weather — summary only
    weather = metrics.get("weather", {})
    if weather and isinstance(weather, dict):
        raw["weather"] = {
            "summary": weather.get("summary", "unknown"),
            "temperature": weather.get("temperature_label", "[REDACTED]"),
            "condition": weather.get("condition", "unknown"),
        }
    else:
        raw["weather"] = {"summary": "no data"}

    # Calendar — count + next event title only
    cal = context_export.get("calendar", {})
    events = cal.get("events", []) if isinstance(cal, dict) else []
    raw["calendar"] = {
        "count": len(events) if isinstance(events, list) else 0,
        "next_event_title": (
            events[0].get("title", "[REDACTED]")
            if isinstance(events, list) and events
            else None
        ),
    }

    # Focus — state and app only
    focus = context_export.get("focus", {})
    if focus and isinstance(focus, dict):
        raw["focus"] = {
            "state": focus.get("state", "unknown"),
            "app": (
                focus.get("active_app", {}).get("name", "unknown")
                if isinstance(focus.get("active_app"), dict)
                else "unknown"
            ),
        }
    else:
        raw["focus"] = {"state": "unknown"}

    # Health — sanitized
    health = context_export.get("health", {})
    raw["health"] = sanitize_health(health) if health else {}

    # Mood — latest score/label
    mood = context_export.get("mood", {})
    if mood and isinstance(mood, dict):
        latest_date = max(mood.keys()) if mood else None
        if latest_date:
            entry = mood[latest_date]
            if isinstance(entry, dict):
                raw["mood"] = {
                    "date": latest_date,
                    "score": entry.get("score"),
                    "label": entry.get("label", "unknown"),
                }
            else:
                raw["mood"] = {}
        else:
            raw["mood"] = {}
    else:
        raw["mood"] = {}

    # Spotify — track/artist only
    spotify = metrics.get("spotify", {})
    if spotify and isinstance(spotify, dict):
        raw["spotify"] = {
            "track": spotify.get("track_name", "unknown"),
            "artist": spotify.get("artist", "unknown"),
            "is_playing": spotify.get("is_playing", False),
        }

    # Reminders count
    reminders = metrics.get("reminders", {})
    if isinstance(reminders, dict):
        raw["reminders_count"] = reminders.get(
            "count", len(reminders) if isinstance(reminders, list) else 0
        )
    else:
        raw["reminders_count"] = 0

    # ── Full sanitization pass ─────────────────────────────────────────
    return sanitize_for_contract(raw)