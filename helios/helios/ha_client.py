"""Helios v6 — Home Assistant REST API client.

Pulls health sensor entities (hae.*) from Home Assistant's /api/states endpoint.
Used by the ingestion pipeline as the primary health data source.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger("helios.ha_client")

# Default health entity prefix — configurable via HA_HEALTH_PREFIX env var
DEFAULT_HA_HEALTH_PREFIX = os.environ.get("HA_HEALTH_PREFIX", "hae.healthsync_")

# ---------------------------------------------------------------------------
# Entity → Helios metric mapping
# ---------------------------------------------------------------------------
HA_TO_HELIOS_MAP: dict[str, tuple[str, float]] = {
    # Sleep metrics
    "sleep_analysis_totalsleep": ("sleep.hours", 1.0),
    "sleep_analysis_core": ("sleep.core_hours", 1.0),
    "sleep_analysis_rem": ("sleep.rem_hours", 1.0),
    "sleep_analysis_deep": ("sleep.deep_hours", 1.0),
    "sleep_analysis_awake": ("sleep.awake_hours", 1.0),
    # Body / vitals
    "resting_heart_rate": ("health.resting_hr", 1.0),
    "heart_rate_variability": ("health.hrv_ms", 1.0),
    "respiratory_rate": ("health.respiratory_rate", 1.0),
    "blood_oxygen_saturation": ("health.blood_o2", 1.0),
    # Activity
    "step_count": ("activity.steps_daily", 1.0),
    "active_energy": ("activity.active_energy_kj", 1.0),
    "apple_exercise_time": ("activity.exercise_minutes", 1.0),
    "walking_running_distance": ("activity.walking_km", 1.0),
    "flights_climbed": ("activity.flights_climbed", 1.0),
    "apple_stand_hour": ("activity.stand_hours", 1.0),
    "apple_stand_time": ("activity.stand_minutes", 1.0),
    # Additional metrics
    "basal_energy_burned": ("health.basal_energy_kj", 1.0),
    "physical_effort": ("activity.physical_effort", 1.0),
    "walking_heart_rate_average": ("health.walking_hr_avg", 1.0),
    "walking_speed": ("activity.walking_speed_kmh", 1.0),
    "walking_step_length": ("activity.walking_step_length_cm", 1.0),
    "walking_asymmetry_percentage": ("activity.walking_asymmetry_pct", 1.0),
    "walking_double_support_percentage": ("activity.walking_double_support_pct", 1.0),
    "stair_speed_down": ("activity.stair_speed_down_ms", 1.0),
    "stair_speed_up": ("activity.stair_speed_up_ms", 1.0),
    "environmental_audio_exposure": ("health.env_audio_db", 1.0),
    "time_in_daylight": ("health.time_in_daylight_min", 1.0),
    "breathing_disturbances": ("health.breathing_disturbances", 1.0),
    "six_minute_walking_test_distance": ("health.six_min_walk_m", 1.0),
    "apple_sleeping_wrist_temperature": ("sleep.wrist_temp_c", 1.0),
}


def _parse_numeric(value_str: str) -> Optional[float]:
    """Parse a HA sensor state string into a float. Returns None for non-numeric."""
    if not value_str or value_str in ("unavailable", "unknown", "None", ""):
        return None
    try:
        return float(value_str)
    except (ValueError, TypeError):
        return None


def fetch_health_entities(
    base_url: str,
    token: str,
    prefix: str = DEFAULT_HA_HEALTH_PREFIX,
    timeout: int = 15,
) -> dict[str, dict[str, Any]]:
    """Fetch all health sensor entities from Home Assistant.

    Args:
        base_url: HA base URL (from config or env)
        token: Long-lived access token
        prefix: Entity ID prefix filter (default: hae.healthsync_)
        timeout: Request timeout in seconds

    Returns:
        Dict keyed by entity suffix (without prefix), each value:
        {value: float|None, unit: str, last_updated: str, entity_id: str}
    """
    url = f"{base_url.rstrip('/')}/api/states"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read()
        all_entities = json.loads(raw)
    except urllib.error.HTTPError as exc:
        logger.warning("HA HTTP error %d: %s", exc.code, exc.reason)
        return {}
    except urllib.error.URLError as exc:
        logger.warning("HA unreachable: %s", exc.reason)
        return {}
    except json.JSONDecodeError as exc:
        logger.warning("HA returned invalid JSON: %s", exc)
        return {}
    except Exception as exc:
        logger.warning("HA fetch failed: %s", exc)
        return {}

    # Filter and parse
    result: dict[str, dict[str, Any]] = {}
    for entity in all_entities:
        eid = entity.get("entity_id", "")
        if not eid.startswith(prefix):
            continue

        # Extract suffix (e.g. "step_count" from "hae.healthsync_step_count")
        suffix = eid[len(prefix):]
        if not suffix:
            continue

        state = entity.get("state", "")
        attrs = entity.get("attributes", {})
        parsed = _parse_numeric(state)

        result[suffix] = {
            "value": parsed,
            "unit": attrs.get("unit_of_measurement", ""),
            "last_updated": entity.get("last_updated", ""),
            "entity_id": eid,
            "friendly_name": attrs.get("friendly_name", ""),
        }

    return result


def fetch_calendar_events(
    base_url: str,
    token: str,
    entity_id: str,
    start: "datetime",
    end: "datetime",
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """Fetch events from a single Home Assistant calendar entity.

    Args:
        base_url: HA base URL (e.g. from config or env)
        token: Long-lived access token
        entity_id: Full entity ID (e.g. from config or env)
        start: Start of range (timezone-aware datetime or ISO string)
        end: End of range (timezone-aware datetime or ISO string)
        timeout: Request timeout in seconds

    Returns:
        List of event dicts with keys: title, location, start_time, end_time,
        is_all_day, source="home_assistant", ha_entity_id, raw_start, raw_end.
        Empty list on any error.
    """
    url = (
        f"{base_url.rstrip('/')}/api/calendars/{entity_id}"
        f"?start={urllib.parse.quote(str(start).replace(' ', 'T'))}"
        f"&end={urllib.parse.quote(str(end).replace(' ', 'T'))}"
    )

    events = _safe_get(url, token, timeout)
    if events is None:
        return []

    results: list[dict[str, Any]] = []
    for evt in events:
        summary = evt.get("summary") or evt.get("title") or "Untitled"
        location = evt.get("location") or ""

        start_info = evt.get("start", {})
        end_info = evt.get("end", {})

        # All-day events use "date": "YYYY-MM-DD", timed use "dateTime": "...T..."
        if "date" in start_info:
            is_all_day = 1
            start_dt = f"{start_info['date']}T00:00:00+00:00"
            end_dt = f"{end_info.get('date', start_info['date'])}T00:00:00+00:00"
        else:
            is_all_day = 0
            start_dt = start_info.get("dateTime", "")
            end_dt = end_info.get("dateTime", "")

        results.append({
            "title": summary,
            "location": location,
            "start_time": start_dt,
            "end_time": end_dt,
            "is_all_day": is_all_day,
            "source": "home_assistant",
            "ha_entity_id": entity_id,
            "raw_start": start_info,
            "raw_end": end_info,
        })

    logger.debug("Fetched %d events from %s", len(results), entity_id)
    return results


def fetch_all_states(
    base_url: str,
    token: str,
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """Fetch all entity states from Home Assistant.

    Args:
        base_url: HA base URL (from config or env)
        token: Long-lived access token
        timeout: Request timeout in seconds

    Returns:
        List of entity dicts from /api/states. Empty list on any error.
    """
    url = f"{base_url.rstrip('/')}/api/states"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read()
        return json.loads(raw)
    except Exception as exc:
        logger.warning("HA fetch_all_states failed: %s", exc)
        return []


def fetch_todo_items(
    base_url: str,
    token: str,
    entity_id: str,
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """Fetch items from a Home Assistant todo list entity.

    Calls the todo.get_items service with return_response.
    Status is filtered to include all items (needs_action + completed).

    Args:
        base_url: HA base URL (from config or env)
        token: Long-lived access token
        entity_id: Full entity ID (e.g. 'todo.my_tasks')
        timeout: Request timeout in seconds

    Returns:
        List of normalized item dicts with keys:
          summary, uid, status, due, completed_ts, source.
        Empty list on any error or when entity has no items.
    """
    url = f"{base_url.rstrip('/')}/api/services/todo/get_items?return_response"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # HA requires entity_id in the request body and ?return_response in the URL
    # Status array gets both active and completed items
    body = json.dumps(
        {
            "entity_id": entity_id,
            "status": ["needs_action", "completed"],
        }
    ).encode()

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        logger.warning("HA todo HTTP %d for %s: %s", exc.code, entity_id, exc.reason)
        return []
    except urllib.error.URLError as exc:
        logger.warning("HA todo unreachable for %s: %s", entity_id, exc.reason)
        return []
    except json.JSONDecodeError as exc:
        logger.warning("HA todo returned invalid JSON for %s: %s", entity_id, exc)
        return []
    except Exception as exc:
        logger.warning("HA todo fetch failed for %s: %s", entity_id, exc)
        return []

    # Response structure:
    #   {"service_response": {"todo.my_tasks": {"items": [...]}}}
    svc_resp = raw.get("service_response", {})
    entity_resp = svc_resp.get(entity_id, {}) if isinstance(svc_resp, dict) else {}
    if isinstance(entity_resp, dict):
        items = entity_resp.get("items", [])
    elif isinstance(entity_resp, list):
        items = entity_resp
    else:
        items = []

    results: list[dict[str, Any]] = []
    for item in items:
        summary = str(item.get("summary") or item.get("title") or "Unnamed Task")[:200]
        status = item.get("status", "needs_action")
        due = item.get("due") or None
        completed_ts = item.get("completed") or None
        uid = str(item.get("uid")) if item.get("uid") else None

        results.append({
            "summary": summary,
            "uid": uid,
            "status": status,
            "due": due,
            "completed_ts": completed_ts,
            "source": entity_id,
        })

    logger.debug("Fetched %d items from %s", len(results), entity_id)
    return results


def check_staleness(
    entities: dict[str, dict[str, Any]],
    max_age_hours: int = 12,
) -> tuple[bool, Optional[str]]:
    """Check if health data is stale.

    Args:
        entities: Result from fetch_health_entities()
        max_age_hours: Maximum age before considering data stale

    Returns:
        (is_stale, newest_timestamp)
    """
    newest = None
    now = datetime.now(timezone.utc)

    for info in entities.values():
        ts_str = info.get("last_updated", "")
        if not ts_str:
            continue
        try:
            # HA returns ISO format: 2026-05-10T17:44:13.998299+00:00
            ts = datetime.fromisoformat(ts_str)
            if newest is None or ts > newest:
                newest = ts
        except (ValueError, TypeError):
            continue

    if newest is None:
        return True, None

    age = now - newest
    is_stale = age > timedelta(hours=max_age_hours)
    return is_stale, newest.isoformat()


# ---------------------------------------------------------------------------
# Convenience: full ingest-ready extraction
# ---------------------------------------------------------------------------

def extract_metrics(
    entities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Convert raw HA entities into Helios-normalized metrics ready for ingestion.

    Returns: {
        "metrics": {helios_metric_name: float_value, ...},
        "source": "home_assistant_health",
        "last_sync": "2026-05-10T17:44:...",
        "health_data_stale": bool,
        "entity_count": int,
        "mapped_count": int,
    }
    """
    metrics: dict[str, float] = {}
    mapped = 0

    for suffix, info in entities.items():
        value = info.get("value")
        if value is None:
            continue

        if suffix in HA_TO_HELIOS_MAP:
            helios_name, multiplier = HA_TO_HELIOS_MAP[suffix]
            metrics[helios_name] = round(value * multiplier, 4)
            mapped += 1
        else:
            # Unmapped — skip (not an error, just not in our mapping)
            logger.debug("Unmapped HA entity: %s (suffix=%s)", info["entity_id"], suffix)

    stale, newest_ts = check_staleness(entities)
    last_sync = newest_ts or ""

    return {
        "metrics": metrics,
        "source": "home_assistant_health",
        "last_sync": last_sync,
        "health_data_stale": stale,
        "entity_count": len(entities),
        "mapped_count": mapped,
    }


# ---------------------------------------------------------------------------
# General-purpose HA REST helpers (v5→v6 migration complete)
# ---------------------------------------------------------------------------

def _safe_get(url: str, token: str, timeout: int = 15) -> Any | None:
    """Safe HTTP GET to HA API. Returns parsed JSON or None on failure."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
        logger.warning("HA GET %s failed: %s", url, exc)
        return None


def _safe_post(url: str, token: str, data: dict | None = None, timeout: int = 15) -> Any | None:
    """Safe HTTP POST to HA API. Returns parsed JSON or None on failure."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = json.dumps(data or {}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
        logger.warning("HA POST %s failed: %s", url, exc)
        return None


def get_token() -> str | None:
    """Resolve HA long-lived access token from environment.

    Checks HASS_TOKEN first, then HA_TOKEN as fallback.
    Returns None if neither is set.
    """
    return os.environ.get("HASS_TOKEN") or os.environ.get("HA_TOKEN") or None


def fetch_state(base_url: str, token: str, entity_id: str, timeout: int = 15) -> dict[str, Any] | None:
    """Fetch a single HA entity state by entity_id."""
    url = f"{base_url.rstrip('/')}/api/states/{entity_id}"
    return _safe_get(url, token, timeout)


def fetch_states(base_url: str, token: str, timeout: int = 15) -> list[dict[str, Any]]:
    """Fetch all HA entity states."""
    url = f"{base_url.rstrip('/')}/api/states"
    result = _safe_get(url, token, timeout)
    if result is None:
        return []
    return result if isinstance(result, list) else []


def fetch_entities_by_domain(base_url: str, token: str, domain: str, timeout: int = 15) -> dict[str, dict[str, Any]]:
    """Filter HA entities by domain prefix (e.g. 'sensor', 'device_tracker')."""
    all_states = fetch_states(base_url, token, timeout)
    prefix = f"{domain}."
    return {s["entity_id"]: s for s in all_states if s.get("entity_id", "").startswith(prefix)}


def fetch_entities_by_prefix(base_url: str, token: str, prefix: str, timeout: int = 15) -> dict[str, dict[str, Any]]:
    """Filter HA entities by arbitrary prefix."""
    all_states = fetch_states(base_url, token, timeout)
    return {s["entity_id"]: s for s in all_states if s.get("entity_id", "").startswith(prefix)}


def call_service(base_url: str, token: str, domain: str, service: str, service_data: dict | None = None, timeout: int = 15) -> Any | None:
    """Call a HA service via the REST API."""
    url = f"{base_url.rstrip('/')}/api/services/{domain}/{service}"
    return _safe_post(url, token, service_data, timeout)


def check_ha_available(base_url: str, token: str, timeout: int = 10) -> dict[str, Any]:
    """Check if HA is reachable and healthy via the /api/ endpoint."""
    url = f"{base_url.rstrip('/')}/api/"
    result = _safe_get(url, token, timeout)
    if result is not None:
        return {
            "available": True,
            "state": "healthy",
            "version": result.get("version", "unknown"),
        }
    return {
        "available": False,
        "state": "unavailable",
    }


def compute_freshness(entity: dict[str, Any]) -> int | None:
    """Compute freshness in seconds since the entity's last_updated timestamp."""
    last_updated = entity.get("last_updated")
    if not last_updated:
        return None
    try:
        if isinstance(last_updated, str):
            dt = datetime.fromisoformat(last_updated)
        else:
            return None
        delta = datetime.now(timezone.utc) - dt
        return int(delta.total_seconds())
    except (ValueError, TypeError):
        return None
