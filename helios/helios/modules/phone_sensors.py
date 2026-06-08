"""Helios v6 — Phone sensor ingestion module (SAN-124).

Reads phone sensor data (steps, battery, screen_time) from the local Health
Auto Export API at http://127.0.0.1:8899 and writes it to metric_snapshots
with source='phone_sensors'.

Designed to be resilient to API format changes — accepts flexible JSON shapes
and extracts what it can.  Uses the Helios CircuitBreaker to back off when the
API is down.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from .base import BaseMod
from ..circuit_breaker import CircuitBreaker

logger = logging.getLogger("helios.modules.phone_sensors")

SOURCE = "phone_sensors"

# Default API endpoints mapping
DEFAULT_ENDPOINTS: dict[str, str] = {
    "steps": "/health/activity",
    "battery": "/health/body",
    "screen_time": "/health/activity",
}

# SQL for upserting metric rows (INSERT OR REPLACE because we want latest-
# value semantics when the same metric+date_key pair is observed again).
_METRIC_UPSERT_SQL = """
INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source, ts)
VALUES (?, ?, ?, ?, ?)
"""


class PhoneSensorsModule(BaseMod):
    """Ingests phone sensor data from the Health Auto Export API into
    metric_snapshots with source='phone_sensors'.

    Supported data:
      - steps  → metric ``phone.steps_daily``
      - battery → metric ``phone.battery_level``
      - screen_time → ``phone.screen_time_minutes``, ``phone.screen_time_pickups``
    """

    encrypted_state = True  # phone metrics are PII

    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "phone_sensors",
        "version": "1.0.0",
        "description": (
            "Ingests phone sensor data (steps, battery, screen_time) from "
            "the Health Auto Export API and writes to metric_snapshots"
        ),
        "author": "system",
        "collectors": ["health_auto_export_api"],
        "dependencies": [],
        "priority": 7,
    }

    def __init__(
        self,
        db_path: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(db_path=db_path, config=config)
        cfg = self.config or {}
        self._api_url: str = (
            cfg.get("api_url")
            or os.environ.get("HELIOS_PHONE_API_URL")
            or "http://127.0.0.1:8899"
        )
        self._timeout: int = int(cfg.get("timeout", 10))
        self._cb = CircuitBreaker(
            failure_threshold=5,
            backoff_schedule=[60, 300, 900, 3600],
        )
        # Track last-seen values for dedup / staleness reporting
        self._last_values: dict[str, Any] = {}

    # ── HTTP helpers ──────────────────────────────────────────────────

    def _get(self, path: str) -> Optional[dict[str, Any]]:
        """GET a JSON endpoint from the Health Auto Export API.

        Respects the circuit breaker — returns None silently when open.
        """
        provider = f"phone_api:{path}"
        if not self._cb.should_attempt(provider):
            logger.debug("Circuit breaker open for %s, skipping", provider)
            return None

        try:
            resp = requests.get(
                f"{self._api_url}{path}",
                timeout=self._timeout,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            self._cb.record_success(provider)
            return resp.json()  # type: ignore[no-any-return]
        except requests.RequestException as exc:
            logger.warning("Phone API %s failed: %s", path, exc)
            self._cb.record_failure(provider)
            return None

    # ── Data extraction ───────────────────────────────────────────────

    @staticmethod
    def _extract_steps(data: dict[str, Any]) -> dict[str, Any]:
        """Extract steps from flexible JSON. Returns {date_key, steps} or {}."""
        # Try direct key
        steps = data.get("steps")
        if steps is not None:
            try:
                steps = int(steps)
            except (ValueError, TypeError):
                steps = None
        # If no top-level steps, look inside activity summary
        if steps is None and isinstance(data.get("activity"), dict):
            steps = data["activity"].get("steps")
            try:
                steps = int(steps) if steps is not None else None
            except (ValueError, TypeError):
                steps = None
        # Last resort: iterate entries looking for step count
        if steps is None and isinstance(data.get("entries"), list):
            for entry in data.get("entries", []):
                if isinstance(entry, dict) and "steps" in entry:
                    try:
                        steps = int(entry["steps"])
                        break
                    except (ValueError, TypeError):
                        continue

        if steps is None:
            return {}

        # Date extraction — flexible
        date_key = (
            data.get("date")
            or data.get("date_key")
            or (data.get("activity", {}) or {}).get("date")
            or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        if isinstance(date_key, str) and len(date_key) >= 10:
            date_key = date_key[:10]

        return {"date_key": date_key, "steps": steps}

    @staticmethod
    def _extract_battery(data: dict[str, Any]) -> dict[str, Any]:
        """Extract battery level from flexible JSON. Returns {timestamp, level, state} or {}."""
        level = data.get("level") or data.get("battery_level") or data.get("battery")
        if level is not None:
            try:
                level = float(level)
            except (ValueError, TypeError):
                level = None

        if level is None:
            # Try inside body dict
            body = data.get("body", {})
            if isinstance(body, dict):
                level = body.get("battery_level") or body.get("level")
                try:
                    level = float(level) if level is not None else None
                except (ValueError, TypeError):
                    level = None

        if level is None:
            return {}

        state = data.get("state") or data.get("battery_state") or "unknown"

        # Battery is a point-in-time reading; use timestamp if available
        ts = (
            data.get("timestamp")
            or data.get("ts")
            or datetime.now(timezone.utc).isoformat()
        )
        # Derive date_key from timestamp if present
        date_key = data.get("date")
        if not date_key and isinstance(ts, str) and len(ts) >= 10:
            date_key = ts[:10]
        if not date_key:
            date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        return {"date_key": date_key, "level": level, "state": state, "ts": ts}

    @staticmethod
    def _extract_screen_time(data: dict[str, Any]) -> dict[str, Any]:
        """Extract screen-time from flexible JSON.

        Returns {date_key, total_minutes, pickups} or {}.
        """
        total_minutes = (
            data.get("total_minutes")
            or data.get("screen_time_minutes")
            or data.get("minutes")
            or data.get("screen_time")
        )
        if total_minutes is not None:
            try:
                total_minutes = float(total_minutes)
            except (ValueError, TypeError):
                total_minutes = None

        # Look inside activity dict if no top-level value
        if total_minutes is None and isinstance(data.get("activity"), dict):
            act = data["activity"]
            total_minutes = (
                act.get("total_minutes") or act.get("screen_time_minutes") or act.get("screen_time")
            )
            try:
                total_minutes = float(total_minutes) if total_minutes is not None else None
            except (ValueError, TypeError):
                total_minutes = None

        if total_minutes is None:
            return {}

        pickups = data.get("pickups") or data.get("pickup_count")
        if pickups is not None:
            try:
                pickups = int(pickups)
            except (ValueError, TypeError):
                pickups = None

        date_key = (
            data.get("date")
            or data.get("date_key")
            or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        if isinstance(date_key, str) and len(date_key) >= 10:
            date_key = date_key[:10]

        return {"date_key": date_key, "total_minutes": total_minutes, "pickups": pickups}

    # ── DB writes ──────────────────────────────────────────────────────

    def _write_metric(
        self,
        conn: sqlite3.Connection,
        metric: str,
        value: float,
        date_key: str,
        ts: Optional[str] = None,
    ) -> None:
        """Insert or replace a metric_snapshots row."""
        conn.execute(
            _METRIC_UPSERT_SQL,
            (metric, value, date_key, SOURCE, ts or datetime.now(timezone.utc).isoformat()),
        )

    def _get_conn(self) -> sqlite3.Connection:
        assert self.db_path, "db_path required for DB writes"
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── Main tick ─────────────────────────────────────────────────────

    def tick(self) -> dict[str, Any]:
        """Run one ingestion pass. Fetches data from the phone sensor API
        and writes it to metric_snapshots.
        """
        result: dict[str, Any] = {"source": SOURCE}

        if not self.db_path:
            result["_warning"] = "No db_path configured"
            return result

        # Fetch all three data types
        steps_data = self._get("/health/activity")
        battery_data = self._get("/health/body")
        screen_time_data = self._get("/health/activity")

        # Extract values
        steps_parsed = self._extract_steps(steps_data) if steps_data else {}
        battery_parsed = self._extract_battery(battery_data) if battery_data else {}
        screen_parsed = self._extract_screen_time(screen_time_data) if screen_time_data else {}

        # Write to DB
        written = 0
        try:
            conn = self._get_conn()
            try:
                # Steps
                if steps_parsed:
                    self._write_metric(
                        conn,
                        "phone.steps_daily",
                        float(steps_parsed["steps"]),
                        steps_parsed["date_key"],
                    )
                    written += 1
                    result["steps"] = steps_parsed["steps"]
                    result["steps_date"] = steps_parsed["date_key"]
                    self._last_values["steps"] = steps_parsed

                # Battery
                if battery_parsed:
                    self._write_metric(
                        conn,
                        "phone.battery_level",
                        float(battery_parsed["level"]),
                        battery_parsed["date_key"],
                        battery_parsed.get("ts"),
                    )
                    written += 1
                    result["battery_level"] = battery_parsed["level"]
                    result["battery_state"] = battery_parsed["state"]
                    result["battery_ts"] = battery_parsed.get("ts")
                    self._last_values["battery"] = battery_parsed

                # Screen time
                if screen_parsed:
                    self._write_metric(
                        conn,
                        "phone.screen_time_minutes",
                        float(screen_parsed["total_minutes"]),
                        screen_parsed["date_key"],
                    )
                    written += 1
                    result["screen_time_minutes"] = screen_parsed["total_minutes"]
                    if screen_parsed.get("pickups") is not None:
                        self._write_metric(
                            conn,
                            "phone.screen_time_pickups",
                            float(screen_parsed["pickups"]),
                            screen_parsed["date_key"],
                        )
                        written += 1
                        result["screen_time_pickups"] = screen_parsed["pickups"]
                    result["screen_time_date"] = screen_parsed["date_key"]
                    self._last_values["screen_time"] = screen_parsed

                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.exception("phone_sensors: DB write failed: %s", exc)
            result["_error"] = str(exc)

        result["metrics_written"] = written
        if written == 0:
            result["_info"] = "No phone sensor data fetched this tick (API may be down or no data available)"

        # Persist state (for circuit breaker state + last values)
        self._persist_state()

        return result

    # ── State persistence ───────────────────────────────────────────────

    def _persist_state(self) -> None:
        """Save module state including circuit breaker and last values."""
        state_data = {
            "circuit_breaker": self._cb.to_dict(),
            "last_values": {
                k: v for k, v in self._last_values.items()
                if isinstance(v, (str, int, float, dict, list, bool))
            },
            "last_tick": datetime.now(timezone.utc).isoformat(),
        }
        self._save_state_encrypted("phone_sensors_state.json", state_data)

    def _load_state(self) -> dict[str, Any]:
        """Load previously saved module state."""
        return self._load_state_encrypted("phone_sensors_state.json")

    def health(self) -> dict[str, Any]:
        """Return health snapshot including circuit breaker status."""
        h = {
            "status": "healthy",
            "name": self.name,
            "api_url": self._api_url,
            "circuit_breaker": self._cb.to_dict(),
            "last_values": {
                k: v for k, v in self._last_values.items()
                if isinstance(v, (str, int, float, dict, list, bool))
            },
        }
        # If any provider circuit is open, report degraded
        for provider, state in self._cb.to_dict().items():
            if state.get("state") == "open":
                h["status"] = "degraded"
                break
        return h