"""Helios v5 — Home Assistant integration bus.

Generic way for Helios to snapshot configured HA entities and expose them
as raw state, without each module duplicating HA client logic.

Writes snapshot to ``~/.hermes/helios/data/home_assistant_state.json``.
All JSON writes are atomic (temp file + rename).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ha_client

logger = logging.getLogger("helios.integration_bus")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
STATE_FILE = DATA_DIR / "home_assistant_state.json"


def _get_token(ha_cfg: dict[str, Any]) -> str:
    """Load HA token from environment.

    Checks token_env config key first, then HASS_TOKEN / HA_TOKEN env vars.
    """
    token_env = ha_cfg.get("token_env", "")
    if token_env:
        token = os.environ.get(token_env, "")
        if token:
            return token
    # Fallback to well-known env vars
    return (
        os.environ.get("HASS_TOKEN", "")
        or os.environ.get("HA_TOKEN", "")
    )


def _compute_freshness(entity: dict[str, Any]) -> int | None:
    """Compute freshness in seconds from an entity's last_updated timestamp.

    Returns None if last_updated is missing or unparseable.
    """
    ts_str = entity.get("last_updated", "")
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str)
        delta = datetime.now(timezone.utc) - ts
        return int(delta.total_seconds())
    except (ValueError, TypeError):
        return None


class HAIntegrationBus:
    """Snapshot configured HA entities into a single JSON state file.

    Args:
        config: The full Helios config dict (top-level keys).
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._ha_cfg: dict[str, Any] = config.get("home_assistant", {})

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Poll HA, write atomic JSON, return the snapshot dict.

        Returns a dict with: source, ts, state, freshness_seconds, entities.
        If HA is disabled or unreachable, returns a minimal dict with
        appropriate state.
        """
        ts = datetime.now(timezone.utc).isoformat()

        if not self._ha_cfg.get("enabled", False):
            result = {
                "source": "home_assistant",
                "ts": ts,
                "state": "disabled",
                "freshness_seconds": None,
                "entities": {},
            }
            self._atomic_write_json(result, str(STATE_FILE))
            return result

        base_url = self._ha_cfg.get("base_url", "")
        token = _get_token(self._ha_cfg)

        if not base_url or not token:
            logger.warning("HA config: missing base_url or token — skipping snapshot")
            result = {
                "source": "home_assistant",
                "ts": ts,
                "state": "failed",
                "freshness_seconds": None,
                "entities": {},
            }
            self._atomic_write_json(result, str(STATE_FILE))
            return result

        # Fetch all states — this also serves as availability check
        all_states = ha_client.fetch_all_states(base_url, token)
        if all_states is None or (isinstance(all_states, (list, dict)) and len(all_states) == 0):
            # HA unreachable or returned no data
            result = {
                "source": "home_assistant",
                "ts": ts,
                "state": "failed",
                "freshness_seconds": None,
                "entities": {},
            }
            self._atomic_write_json(result, str(STATE_FILE))
            return result

        # Filter entities
        entities: dict[str, dict] = {}
        freshness_values: list[int] = []
        stale_hours = self._ha_cfg.get("timeout", 15)

        for entity in all_states:
            eid = entity.get("entity_id", "")
            if not self._should_include(eid):
                continue

            freshness = _compute_freshness(entity)
            # Remove sensitive/large attribute data to keep snapshot lean
            attrs = entity.get("attributes", {})
            # Keep only non-sensitive, reasonably-sized attributes
            safe_attrs = {
                k: v for k, v in attrs.items()
                if not k.lower().endswith("_token") and not k.lower().endswith("_key")
                and not k.lower().endswith("_password")
                and len(str(v)) < 500
            }

            entity_entry: dict[str, Any] = {
                "state": entity.get("state"),
                "attributes": safe_attrs,
                "last_updated": entity.get("last_updated"),
            }

            if freshness is not None:
                entity_entry["freshness_seconds"] = freshness
                entity_entry["confidence"] = self._compute_confidence(freshness)
                freshness_values.append(freshness)
            else:
                entity_entry["freshness_seconds"] = None
                entity_entry["confidence"] = 0.0

            entities[eid] = entity_entry

        # Compute overall freshness and state
        overall_freshness: int | None
        if freshness_values:
            overall_freshness = min(freshness_values)
        else:
            overall_freshness = None

        overall_state = self._compute_overall_state(entities, overall_freshness)

        result = {
            "source": "home_assistant",
            "ts": ts,
            "state": overall_state,
            "freshness_seconds": overall_freshness,
            "entities": entities,
        }

        self._atomic_write_json(result, str(STATE_FILE))
        logger.debug("HA snapshot: %d entities, state=%s, freshness=%ss", len(entities), overall_state, overall_freshness)
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _should_include(self, entity_id: str) -> bool:
        """Return True if entity_id should be included in the snapshot.

        Filters by ``home_assistant.sensors.include_domains`` config.
        If no include_domains specified, includes all entities.
        """
        include_domains = self._ha_cfg.get("sensors", {}).get("include_domains", [])
        if not include_domains:
            return True
        # entity_id format is "domain.name" — check if domain is in include list
        domain = entity_id.split(".")[0] if "." in entity_id else entity_id
        return domain in include_domains

    def _compute_confidence(self, freshness_seconds: int, stale_hours: int = 12) -> float:
        """Compute confidence score from freshness.

        1.0 for data updated <60s ago, decays linearly to 0.0 at *stale_hours*.
        """
        if freshness_seconds < 60:
            return 1.0
        stale_seconds = stale_hours * 3600
        if freshness_seconds >= stale_seconds:
            return 0.0
        return max(0.0, 1.0 - (freshness_seconds - 60) / (stale_seconds - 60))

    def _compute_overall_state(self, entities: dict[str, dict], overall_freshness: int | None) -> str:
        """Derive the overall HA health state from entity freshness."""
        if not entities:
            return "unknown"
        if overall_freshness is None:
            return "unknown"

        # Count entities by health
        stale_hours = self._ha_cfg.get("timeout", 15)
        stale_threshold = stale_hours * 3600

        fresh_count = 0
        stale_count = 0
        for entry in entities.values():
            f = entry.get("freshness_seconds")
            if f is not None and f < stale_threshold:
                fresh_count += 1
            else:
                stale_count += 1

        total = fresh_count + stale_count
        if total == 0:
            return "unknown"

        fresh_ratio = fresh_count / total
        if fresh_ratio > 0.8:
            return "healthy"
        if fresh_ratio > 0.5:
            return "degraded"
        return "stale"

    def _atomic_write_json(self, data: dict[str, Any], path: str) -> None:
        """Write data as JSON atomically using temp file + rename."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)

        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(target.parent),
                suffix=".tmp",
            )
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp_path, str(target))
        except Exception as exc:
            logger.warning("Failed to write HA state to %s: %s", path, exc)
            # Clean up temp file if rename failed
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass