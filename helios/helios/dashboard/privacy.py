"""Privacy sanitization for dashboard data.

All data served by the dashboard goes through these sanitizers before
reaching the API layer or static page. The goal is to expose operational
visibility without leaking secrets, personal details, or sensitive content.

Privacy classes:
    PUBLIC_SAFE    — No sanitization needed (timestamps, counts, health labels)
    AGENT_SAFE     — Summarized for agent use (module names, freshness, confidence)
    PRIVATE        — Redact values, keep keys (metrics, location city only)
    HIGHLY_SENSITIVE — Omit entirely (tokens, passwords, room IDs, raw health)
    NEVER_EXPORT   — Never appear in any export (credentials, API keys, raw email)
"""

from __future__ import annotations

import re
from typing import Any

# ── Privacy classes ─────────────────────────────────────────────────────────

PUBLIC_SAFE = "public_safe"
AGENT_SAFE = "agent_safe"
PRIVATE = "private"
HIGHLY_SENSITIVE = "highly_sensitive"
NEVER_EXPORT = "never_export"

# ── Fields that must never appear in dashboard output ────────────────────────

NEVER_EXPORT_FIELDS: frozenset[str] = frozenset({
    "token", "access_token", "refresh_token", "api_key", "secret",
    "password", "cookie", "session_id", "authorization",
    "homeserver", "room_id", "matrix_room", "push_token",
    "webhook_url", "oauth", "credential",
})

# ── Fields that should have values redacted ───────────────────────────────────

PRIVATE_FIELDS: frozenset[str] = frozenset({
    "latitude", "longitude", "lat", "lon", "alt", "accuracy",
    "address", "street", "zip", "postal_code",
    "phone", "email", "sender", "recipient",
    "body", "snippet", "subject", "raw_body", "raw_ref",
    "display_name", "full_name", "contact_name",
    # Coordinate-adjacent keys that could leak numeric pairs
    "position", "worksite_key", "coords", "coordinates", "gps",
})

# ── Patterns for redaction ────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"(token|key|secret|password|cookie|auth|bearer)\s*[:=]\s*\S+", re.IGNORECASE)
_COORD_RE = re.compile(r"-?\d{1,3}\.\d{4,}")
_EMAIL_RE = re.compile(r"[\w.-]+@[\w.-]+\.\w+")
_ROOM_ID_RE = re.compile(r"![\w-]+:[\w.-]+")

# Compiled pattern for matching private field names as word-boundary substrings
# in dictionary keys.  E.g. "coords" should NOT match "counts", but SHOULD
# match "raw_coords", "coords_home", "home_coords".
_PRIVATE_FIELD_RES = [re.compile(rf"(?:^|_){re.escape(pf)}(?:$|_)") for pf in PRIVATE_FIELDS]


def _key_matches_private_field(key_lower: str) -> bool:
    """Return True if *key_lower* matches any PRIVATE_FIELDS word-boundary pattern.

    Matches:
      - Exact match:  "lat" matches "lat"
      - Prefixed:     "raw_body" matches "body"
      - Suffixed:     "lat_home" matches "lat"
      - Both:         "home_lat_primary" matches "lat"

    Does NOT match:
      - Substring:    "counts" does NOT match "coords"
    """
    for pf in PRIVATE_FIELDS:
        if key_lower == pf:
            return True
        for pattern in _PRIVATE_FIELD_RES:
            if pattern.search(key_lower):
                return True
    return False


def _looks_like_coordinate_pair(value: list) -> bool:
    """Return True if a list contains exactly 2 floats that look like lat/lon.

    A coordinate pair is two numbers both in [-180, 180] where at least one
    has a fractional part or absolute value > 10.  Small integers like [3, 7]
    are NOT coordinate pairs.
    """
    if len(value) != 2:
        return False
    try:
        a, b = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return False
    if not (-180.0 <= a <= 180.0 and -180.0 <= b <= 180.0):
        return False
    # Require at least one value to look like a real coordinate:
    # either has a fractional part or is large enough to be a lat/lon
    has_fractional = (a != int(a) or b != int(b))
    is_large = (abs(a) > 10 or abs(b) > 10)
    return has_fractional or is_large


def sanitize_dict(data: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Recursively sanitize a dictionary. Removes never-export fields,
    redacts private field values, detects coordinate lists, and keeps
    public/agent-safe fields intact."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        key_lower = key.lower()

        # Never export
        if any(nf in key_lower for nf in NEVER_EXPORT_FIELDS):
            continue

        # Private fields: keep key, redact value unconditionally
        if _key_matches_private_field(key_lower):
            result[key] = "[REDACTED]"
            continue

        # Recurse into nested dicts
        if isinstance(value, dict):
            result[key] = sanitize_dict(value, depth + 1)
        elif isinstance(value, list):
            # Check if the list itself looks like a coordinate pair
            if _looks_like_coordinate_pair(value):
                result[key] = "[REDACTED]"
            else:
                result[key] = [
                    sanitize_dict(item, depth + 1) if isinstance(item, dict) else item
                    for item in value
                ]
        elif isinstance(value, str):
            result[key] = _redact_string(value)
        else:
            result[key] = value

    return result


def _redact_string(s: str) -> str:
    """Redact sensitive content from a string, preserving structure."""
    s = _TOKEN_RE.sub("[TOKEN_REDACTED]", s)
    s = _COORD_RE.sub("[COORD]", s)
    s = _EMAIL_RE.sub("[EMAIL]", s)
    s = _ROOM_ID_RE.sub("[ROOM_ID]", s)
    return s


def sanitize_location(loc: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a location dict: keep city/region/country, redact coordinates."""
    if not loc:
        return {}
    safe: dict[str, Any] = {}
    for key in ("city", "region", "country", "is_home", "source",
                "last_updated", "stale", "stale_secs", "confidence"):
        if key in loc:
            safe[key] = loc[key]
    # Include a safe label instead of exact coordinates
    if "city" in loc:
        safe["location_label"] = loc["city"]
    elif "region" in loc:
        safe["location_label"] = loc["region"]
    else:
        safe["location_label"] = "[REDACTED]"
    return safe


def sanitize_health(health: dict[str, Any]) -> dict[str, Any]:
    """Keep health labels and scores, redact raw metric values."""
    if not health:
        return {}
    safe: dict[str, Any] = {}
    for key, value in health.items():
        key_lower = key.lower()
        if any(nf in key_lower for nf in NEVER_EXPORT_FIELDS):
            continue
        # Keep scores and labels
        if any(k in key_lower for k in ("score", "label", "status", "state", "category")):
            safe[key] = value
        else:
            # Redact the actual value but keep the key for structure
            safe[key] = "[REDACTED]"
    return safe


def privacy_panel() -> list[dict[str, str]]:
    """Return the privacy class catalog for the dashboard panel."""
    return [
        {"class": PUBLIC_SAFE, "description": "No sanitization needed — timestamps, counts, labels"},
        {"class": AGENT_SAFE, "description": "Summarized for agent use — module names, freshness, confidence"},
        {"class": PRIVATE, "description": "Keys kept, values redacted — metrics, location city only"},
        {"class": HIGHLY_SENSITIVE, "description": "Omitted entirely — tokens, passwords, room IDs, raw health"},
        {"class": NEVER_EXPORT, "description": "Never appear in any export — credentials, API keys, raw email"},
    ]