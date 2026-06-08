"""Helios v6 — Health module (reads from metric_snapshots populated by HA ingestion).

Source of truth: Home Assistant hae.* sensors → metric_snapshots (source=home_assistant_health).
Old JSON files (~/.hermes/health_data/) are no longer read directly.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from .base import BaseMod

logger = logging.getLogger("helios.modules.health")


class HealthModule(BaseMod):
    encrypted_state = True  # Health metrics are PII

    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "health",
        "version": "2.0.0",
        "description": "Tracks health metrics from Home Assistant health sensors via metric_snapshots",
        "author": "system",
        "collectors": ["home_assistant_health"],
        "dependencies": [],
        "priority": 7,
    }

    # Metrics to surface to the module context
    SURFACE_METRICS = [
        "sleep.hours",
        "sleep.core_hours",
        "sleep.rem_hours",
        "sleep.deep_hours",
        "sleep.awake_hours",
        "health.resting_hr",
        "health.hrv_ms",
        "health.respiratory_rate",
        "health.blood_o2",
        "activity.steps_daily",
        "activity.active_energy_kj",
        "activity.minutes_daily",
        "activity.walking_km",
        "activity.stand_hours",
    ]

    def __init__(self, db_path: str, config: Optional[dict[str, Any]] = None) -> None:
        super().__init__(db_path, config)

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def tick(self) -> dict[str, Any]:
        conn = self._get_conn()
        data: dict[str, Any] = {
            "source": "home_assistant_health",
            "records": 0,
            "latest": {},
        }
        stale_hours = self.config.get("stale_hours", 12) if self.config else 12

        try:
            # ---------------------------------------------------------------
            # 1. Read today's HA health metrics from metric_snapshots
            # ---------------------------------------------------------------
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            rows = conn.execute(
                """SELECT metric, value, ts
                   FROM metric_snapshots
                   WHERE source = 'home_assistant_health'
                     AND date_key = ?
                   ORDER BY ts""",
                (today_str,),
            ).fetchall()

            if rows:
                metrics_snapshot = {}
                for row in rows:
                    if row["metric"] == "health.ha_last_sync_epoch":
                        # Store sync timestamp separately
                        data["last_sync_epoch"] = row["value"]
                        continue
                    metrics_snapshot[row["metric"]] = row["value"]
                    # Surface key metrics directly on the data dict
                    if row["metric"] in self.SURFACE_METRICS:
                        data[row["metric"]] = row["value"]

                data["records"] = len(rows)
                data["latest"] = metrics_snapshot

                # ---------------------------------------------------------------
                # 2. Staleness check — read the sync timestamp from ingestion
                # ---------------------------------------------------------------
                sync_row = conn.execute(
                    """SELECT value, ts FROM metric_snapshots
                       WHERE source = 'home_assistant_health'
                         AND metric = 'health.ha_last_sync_epoch'
                         AND date_key = ?
                       ORDER BY ts DESC LIMIT 1""",
                    (today_str,),
                ).fetchone()

                if sync_row:
                    sync_epoch = sync_row["value"]
                    sync_dt = datetime.fromtimestamp(sync_epoch, tz=timezone.utc)
                    data["last_sync"] = sync_dt.isoformat()
                    age = datetime.now(timezone.utc) - sync_dt
                    data["health_data_stale"] = age > timedelta(hours=stale_hours)

                    # Report freshness_secs for module health tracking.
                    # Health Auto Export delivers 1-2 batches per day via HA (iOS
                    # pushes when the app syncs, typically every 8-24 h).  The
                    # previous thresholds (8/16/24 h) caused constant "stale"
                    # alerts because the data is legitimately batched, not real-
                    # time.  New thresholds reflect actual delivery cadence:
                    #   fresh    = 24 h — within normal daily batch window
                    #   stale    = 48 h — missed a full day
                    #   degraded = 72 h — two days without data
                    data["freshness_secs"] = age.total_seconds()
                    data["_freshness_threshold_override"] = {
                        "fresh": 86400,    # 24 h — normal batch interval
                        "stale": 172800,   # 48 h — missed a full day
                        "degraded": 259200, # 72 h — two days without data
                    }

                    if data["health_data_stale"]:
                        data["_warning"] = (
                            f"Health data is stale: last sync was "
                            f"{round(age.total_seconds() / 3600, 1)} hours ago "
                            f"(threshold: {stale_hours}h)"
                        )
                        logger.warning(
                            "Health data stale: last_sync=%s, age=%.1fh, threshold=%dh",
                            sync_dt.isoformat(),
                            age.total_seconds() / 3600,
                            stale_hours,
                        )
                else:
                    # No sync marker found — data may not have been ingested yet
                    data["last_sync"] = None
                    data["health_data_stale"] = True

            # ---------------------------------------------------------------
            # 3. Fallback: check if any metrics exist at all (across all dates)
            # ---------------------------------------------------------------
            if data["records"] == 0:
                total_rows = conn.execute(
                    "SELECT COUNT(*) FROM metric_snapshots WHERE source = 'home_assistant_health'"
                ).fetchone()[0]

                if total_rows == 0:
                    data["_info"] = (
                        "No HA health data in metric_snapshots yet. "
                        "Data arrives via HA automation → Helios ingestion. "
                        "If this persists, check HA health_prefix config and HASS_TOKEN."
                    )
                else:
                    data["_info"] = (
                        f"No health data for today ({today_str}). "
                        f"{total_rows} historical rows exist."
                    )

        except Exception as exc:
            logger.exception("Health module tick failed: %s", exc)
            data["_error"] = str(exc)
        finally:
            conn.close()

        return data
