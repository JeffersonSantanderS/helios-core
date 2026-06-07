"""Helios v7 — Module freshness and confidence standardization.

Every module-owned module should emit standard freshness/confidence fields
in its tick() output. This module provides helpers and a standard contract.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("helios.freshness")

# Standard confidence levels
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_UNKNOWN = "unknown"
CONFIDENCE_NEEDS_REVIEW = "needs_review"

VALID_CONFIDENCES = frozenset({
    CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW,
    CONFIDENCE_UNKNOWN, CONFIDENCE_NEEDS_REVIEW,
})

# Staleness thresholds (seconds)
STALE_THRESHOLDS = {
    "location": 300,       # 5 minutes
    "calendar": 1800,      # 30 minutes
    "health": 43200,       # 12 hours
    "spotify": 300,        # 5 minutes
    "tasks": 1800,         # 30 minutes
    "reminders": 1800,     # 30 minutes
    "mood": 86400,         # 24 hours
    "focus": 600,          # 10 minutes
    "work_hours": 3600,    # 1 hour
    "weather": 1800,       # 30 minutes
    "home": 300,           # 5 minutes
    "contacts": 86400,     # 24 hours
    "notes": 86400,        # 24 hours
    "nutrition": 86400,    # 24 hours
    "habits": 86400,       # 24 hours
    "server_health": 300,  # 5 minutes
    "system_health": 300,  # 5 minutes
}

DEFAULT_STALE_SECS = 3600  # 1 hour


@dataclass(frozen=True)
class FreshnessResult:
    """Standard freshness/confidence output for a module."""
    source: str
    last_updated: Optional[str]
    freshness_secs: Optional[float]
    confidence: str
    warning: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "last_updated": self.last_updated,
            "freshness_secs": self.freshness_secs,
            "confidence": self.confidence,
            "_warning": self.warning,
        }


def compute_confidence(
    module_name: str,
    freshness_secs: Optional[float],
    data_present: bool = True,
    error_count: int = 0,
) -> str:
    """Compute confidence level from freshness and data quality.

    Args:
        module_name: Module name for staleness threshold lookup.
        freshness_secs: Seconds since last update. None = unknown.
        data_present: Whether meaningful data exists.
        error_count: Number of recent errors.

    Returns:
        One of CONFIDENCE_HIGH/MEDIUM/LOW/UNKNOWN/NEEDS_REVIEW.
    """
    if error_count > 3:
        return CONFIDENCE_NEEDS_REVIEW
    if not data_present:
        return CONFIDENCE_UNKNOWN
    if freshness_secs is None:
        return CONFIDENCE_UNKNOWN

    threshold = STALE_THRESHOLDS.get(module_name, DEFAULT_STALE_SECS)

    if freshness_secs <= threshold * 0.5:
        return CONFIDENCE_HIGH
    elif freshness_secs <= threshold:
        return CONFIDENCE_MEDIUM
    elif freshness_secs <= threshold * 3:
        return CONFIDENCE_LOW
    else:
        return CONFIDENCE_NEEDS_REVIEW


def compute_freshness(
    last_updated_str: Optional[str],
    now: Optional[datetime] = None,
) -> Optional[float]:
    """Compute freshness in seconds from an ISO timestamp.

    Args:
        last_updated_str: ISO datetime string or None.
        now: Override current time (for testing).

    Returns:
        Seconds since the timestamp, or None if unavailable.
    """
    if not last_updated_str:
        return None
    try:
        ts = datetime.fromisoformat(last_updated_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ref = now or datetime.now(timezone.utc)
        return max(0.0, (ref - ts).total_seconds())
    except (ValueError, TypeError):
        return None


def assess_module(
    module_name: str,
    tick_output: dict[str, Any],
    now: Optional[datetime] = None,
) -> FreshnessResult:
    """Assess a module's tick output for freshness and confidence.

    Standardizes the output to include source, last_updated,
    freshness_secs, and confidence. Adds _warning for stale data.

    Args:
        module_name: The module name.
        tick_output: The dict returned by module.tick().
        now: Override current time (for testing).

    Returns:
        FreshnessResult with standardized fields.
    """
    source = tick_output.get("source", "unknown")
    last_updated = tick_output.get("last_updated")
    freshness_secs = tick_output.get("freshness_secs")

    # Recompute if not provided
    if freshness_secs is None and last_updated:
        freshness_secs = compute_freshness(last_updated, now)

    # Compute confidence
    existing_conf = tick_output.get("confidence")
    if existing_conf and existing_conf in VALID_CONFIDENCES:
        confidence = existing_conf
    else:
        confidence = compute_confidence(
            module_name, freshness_secs,
            data_present=bool(tick_output and tick_output.get("_error") is None),
        )

    # Generate warning for stale data
    warning = None
    if freshness_secs is not None:
        threshold = STALE_THRESHOLDS.get(module_name, DEFAULT_STALE_SECS)
        if freshness_secs > threshold:
            warning = f"{module_name} data is {freshness_secs:.0f}s old (threshold: {threshold}s)"

    return FreshnessResult(
        source=source,
        last_updated=last_updated,
        freshness_secs=freshness_secs,
        confidence=confidence,
        warning=warning,
    )


def standardize_module_output(
    module_name: str,
    tick_output: dict[str, Any],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Add standard freshness/confidence fields to a module tick output.

    Does not remove any existing fields. Adds or updates:
        - source
        - last_updated
        - freshness_secs
        - confidence
        - _warning (if stale)

    Args:
        module_name: The module name.
        tick_output: The dict returned by module.tick().
        now: Override current time (for testing).

    Returns:
        Updated tick_output dict with standard fields.
    """
    result = dict(tick_output)
    assessment = assess_module(module_name, result, now)

    # Only update if not already set or if we can add missing fields
    if "source" not in result:
        result["source"] = assessment.source
    if "last_updated" not in result:
        result["last_updated"] = assessment.last_updated
    if "freshness_secs" not in result:
        result["freshness_secs"] = assessment.freshness_secs
    if "confidence" not in result:
        result["confidence"] = assessment.confidence
    if assessment.warning:
        result["_warning"] = assessment.warning

    return result