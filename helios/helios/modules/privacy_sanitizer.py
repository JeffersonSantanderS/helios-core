"""Helios v6 — Privacy Sanitizer for location events.

Ensures raw lat/lon, coordinate pairs, and place lookup internals
never leak into logs, exports, notifications, or self-improvement events.
"""
from typing import Any
import copy
import re

# Patterns that identify raw coordinate values or coordinate-pair structures
COORD_PATTERNS = [
    re.compile(r"^\s*\[" + r"[-\d.]+\s*,\s*[-\d.]+" + r"\]\s*$"),
    re.compile(r"lat\s*[:=]\s*[-\d.]+"),
    re.compile(r"lon\s*[:=]\s*[-\d.]+"),
    re.compile(r"latitude\s*[:=]\s*[-\d.]+"),
    re.compile(r"longitude\s*[:=]\s*[-\d.]+"),
]

# Keys that are always removed from exported/sanitized objects
SENSITIVE_KEYS = frozenset([
    "lat", "lon", "latitude", "longitude", "accuracy", "altitude",
    "gps_accuracy", "vertical_accuracy", "course", "speed",
    "horizontalAccuracy", "key",  # poi_memory key is a raw coord pair
])

# Redaction sentinel
REDACTED = "<REDACTED>"


def redact_sensitive_keys(obj: Any) -> Any:
    """Recursively remove or redact sensitive keys from a dict/list structure."""
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for k, v in obj.items():
            if k in SENSITIVE_KEYS:
                continue  # drop entirely
            result[k] = redact_sensitive_keys(v)
        return result
    if isinstance(obj, list):
        return [redact_sensitive_keys(v) for v in obj]
    if isinstance(obj, str):
        # Strip inline coordinate pairs like "[51.16, -113.96]"
        for pat in COORD_PATTERNS:
            obj = pat.sub("<coord>", obj)
        return obj
    return obj


def sanitize_location_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return a privacy-safe copy of a location event for exports/notifications.

    Keeps: zone, place_name, place_type, city, province, source, ts
    Drops: lat, lon, accuracy, any raw coordinate pairs
    """
    safe = copy.deepcopy(event)
    safe = redact_sensitive_keys(safe)

    # If place_name is present, we downgrade source to avoid leaking
    # that a network lookup was involved.
    if safe.get("place_name") and safe.get("source") == "overpass":
        safe["source"] = "inferred"

    # Ensure no internal keys survive
    for k in ["dwell_seconds", "_dwell_buffer", "_poi_key", "_visit_key"]:
        safe.pop(k, None)

    return safe


def sanitize_log_message(msg: str) -> str:
    """Redact raw coordinates from a log line before emitting."""
    # Replace [51.1628, -113.9553] patterns
    msg = re.sub(
        r"\[\s*[-\d.]+\s*,\s*[-\d.]+\s*\]",
        "[<coord-redacted>]",
        msg,
    )
    # Replace lat/lon key=value pairs
    msg = re.sub(
        r"(lat|lon|latitude|longitude)\s*[:=]\s*[-\d.]+",
        r"\1=<redacted>",
        msg,
    )
    return msg
