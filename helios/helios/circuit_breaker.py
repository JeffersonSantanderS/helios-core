"""Helios v5 — Circuit breaker."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

log = logging.getLogger("helios.circuit_breaker")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"
DEGRADED = "degraded"

BACKOFF_SCHEDULE = [60, 300, 900, 3600]


class CircuitBreaker:
    """Per-provider failure tracking with backoff."""

    def __init__(self, failure_threshold: int = 5, backoff_schedule: Optional[list[int]] = None):
        self._threshold = failure_threshold if failure_threshold is not None else 5
        self._backoff = list(backoff_schedule) if backoff_schedule else list(BACKOFF_SCHEDULE)
        self._providers: dict[str, dict[str, Any]] = {}

    def state(self, provider: str) -> str:
        st = self._providers.get(provider, {})
        current = st.get("state", CLOSED)
        if current == OPEN:
            opened_at = st.get("opened_at", 0)
            idx = min(st.get("backoff_idx", 0), len(self._backoff) - 1)
            if time.time() - opened_at >= self._backoff[idx]:
                return HALF_OPEN
        return current

    def record_success(self, provider: str) -> None:
        self._providers[provider] = {"state": CLOSED, "failures": 0, "backoff_idx": 0}

    def record_failure(self, provider: str) -> None:
        st = self._providers.setdefault(provider, {"state": CLOSED, "failures": 0, "backoff_idx": 0})
        st["failures"] = (st.get("failures") or 0) + 1
        if st["failures"] >= (self._threshold or 5):
            st["state"] = OPEN
            st["opened_at"] = time.time()
        elif st["failures"] >= 2:
            st["state"] = DEGRADED

    def should_attempt(self, provider: str) -> bool:
        return self.state(provider) in (CLOSED, HALF_OPEN)

    def to_dict(self) -> dict[str, Any]:
        return dict(self._providers)

    def from_dict(self, data: dict[str, Any]) -> None:
        self._providers = dict(data)
