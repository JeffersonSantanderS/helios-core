"""Candidate builders — convert raw inputs into normalized Candidates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Candidate
from .config import PriorityConfig


class CandidateBuilder:
    """Orchestrate candidate generation from multiple sources."""

    def __init__(self, cfg: PriorityConfig):
        self.cfg = cfg
        self.sources: list[CandidateSource] = []

    def register(self, source: CandidateSource) -> None:
        self.sources.append(source)

    def from_tick(
        self,
        tick_id: str,
        context: dict[str, Any],
        rule_hits: list[dict] | None = None,
        source_events: list[dict] | None = None,
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for src in self.sources:
            if not src.enabled(self.cfg):
                continue
            try:
                cands = src.generate(
                    tick_id=tick_id,
                    context=context,
                    rule_hits=rule_hits or [],
                    source_events=source_events or [],
                )
                candidates.extend(cands)
            except Exception:
                continue
        return candidates


class CandidateSource:
    """Base class for candidate generation sources."""

    def enabled(self, cfg: PriorityConfig) -> bool:
        return True

    def generate(
        self,
        tick_id: str,
        context: dict[str, Any],
        rule_hits: list[dict],
        source_events: list[dict],
    ) -> list[Candidate]:
        raise NotImplementedError


class RuleHitCandidateSource(CandidateSource):
    """Convert rule_v2 hits into Candidates."""

    def enabled(self, cfg: PriorityConfig) -> bool:
        return cfg.sources.rules

    def generate(
        self,
        tick_id: str,
        context: dict[str, Any],
        rule_hits: list[dict],
        source_events: list[dict],
    ) -> list[Candidate]:
        now = datetime.now(timezone.utc).isoformat()
        candidates: list[Candidate] = []
        for hit in rule_hits:
            if not isinstance(hit, dict):
                continue
            slug = hit.get("slug", "rule_hit")
            title = hit.get("title", slug.replace("_", " ").title())
            message = hit.get("message", "")
            severity = hit.get("severity", "info")
            category = hit.get("category", "system")
            priority = hit.get("priority", 1)
            module = hit.get("module", "")
            action_config = hit.get("action", hit.get("action_config", {}))
            raw = dict(hit)

            cand = Candidate(
                candidate_id=Candidate.make_id(),
                tick_id=tick_id,
                created_at=now,
                source="rules_v2",
                candidate_type="rule_alert",
                title=title,
                message=message,
                severity=severity,
                category=category,
                priority_hint=priority,
                module=module,
                rule_slug=slug,
                action_name=raw.get("action", ""),
                action_config=action_config if isinstance(action_config, dict) else {},
                raw_payload=raw,
                tags=["rule", category, severity],
                fingerprint=Candidate.make_fingerprint(
                    candidate_type="rule_alert",
                    source="rules_v2",
                    rule_slug=slug,
                    module=module,
                    title=title,
                    raw_payload=raw,
                ),
            )
            candidates.append(cand)
        return candidates


class HomeCandidateSource(CandidateSource):
    """Generate candidates from home module environmental state."""

    def enabled(self, cfg: PriorityConfig) -> bool:
        return cfg.sources.home

    def generate(
        self,
        tick_id: str,
        context: dict[str, Any],
        rule_hits: list[dict],
        source_events: list[dict],
    ) -> list[Candidate]:
        now = datetime.now(timezone.utc).isoformat()
        candidates: list[Candidate] = []
        home = context.get("home", {})
        if not isinstance(home, dict):
            return candidates

        # 1. Lights on while no rooms occupied — but only if nobody is home
        rooms_occupied = home.get("rooms_occupied")
        total_lights_on = home.get("total_lights_on")
        anyone_home = home.get("anyone_home", False)
        if rooms_occupied == 0 and total_lights_on and total_lights_on > 2 and not anyone_home:
            candidates.append(Candidate(
                candidate_id=Candidate.make_id(),
                tick_id=tick_id,
                created_at=now,
                source="home_sensor",
                candidate_type="home_environment_alert",
                title="Lights on but house appears empty",
                message=f"{total_lights_on} lights on, 0 rooms occupied.",
                severity="info",
                category="home",
                priority_hint=1,
                module="home",
                raw_payload={"rooms_occupied": rooms_occupied, "total_lights_on": total_lights_on},
                tags=["home", "energy", "lights"],
                fingerprint=Candidate.make_fingerprint(
                    candidate_type="home_environment_alert",
                    source="home_sensor",
                    module="home",
                    title="lights_on_empty_house",
                    raw_payload={"rooms_occupied": rooms_occupied, "total_lights_on": total_lights_on},
                ),
            ))

        # 2. Room occupied but lights off
        for room_id in ("master_bedroom", "spare_bedroom"):
            occupied = home.get(f"{room_id}_occupied")
            light_count = home.get(f"{room_id}_light_count")
            lux = home.get(f"{room_id}_lux")
            if occupied and light_count == 0 and lux is not None and lux < 10:
                candidates.append(Candidate(
                    candidate_id=Candidate.make_id(),
                    tick_id=tick_id,
                    created_at=now,
                    source="home_sensor",
                    candidate_type="home_environment_alert",
                    title=f"{room_id.replace('_', ' ').title()} occupied but dark",
                    message=f"Room occupied, {light_count} lights, {lux} lux.",
                    severity="info",
                    category="home",
                    priority_hint=1,
                    module="home",
                    raw_payload={"room": room_id, "occupied": occupied, "light_count": light_count, "lux": lux},
                    tags=["home", "occupancy", "lights", room_id],
                    fingerprint=Candidate.make_fingerprint(
                        candidate_type="home_environment_alert",
                        source="home_sensor",
                        module="home",
                        title="room_occupied_dark",
                        raw_payload={"room": room_id},
                    ),
                ))

        # 3. Temperature unusually high / low (no rule needed for extreme values)
        for room_id, threshold_high, threshold_low in (
            ("master_bedroom", 27, 16),
            ("spare_bedroom", 28, 16),
        ):
            temp = home.get(f"{room_id}_temp_c")
            if temp is not None:
                if temp > threshold_high:
                    candidates.append(Candidate(
                        candidate_id=Candidate.make_id(),
                        tick_id=tick_id,
                        created_at=now,
                        source="home_sensor",
                        candidate_type="home_environment_alert",
                        title=f"{room_id.replace('_', ' ').title()} temperature high",
                        message=f"{temp:.1f}°C — above comfortable threshold.",
                        severity="warning",
                        category="home",
                        priority_hint=2,
                        module="home",
                        raw_payload={"room": room_id, "temp_c": temp, "threshold": threshold_high},
                        tags=["home", "temperature", room_id],
                        fingerprint=Candidate.make_fingerprint(
                            candidate_type="home_environment_alert",
                            source="home_sensor",
                            module="home",
                            title="room_temp_high",
                            raw_payload={"room": room_id, "threshold": threshold_high},
                        ),
                    ))
                elif temp < threshold_low:
                    candidates.append(Candidate(
                        candidate_id=Candidate.make_id(),
                        tick_id=tick_id,
                        created_at=now,
                        source="home_sensor",
                        candidate_type="home_environment_alert",
                        title=f"{room_id.replace('_', ' ').title()} temperature low",
                        message=f"{temp:.1f}°C — below comfortable threshold.",
                        severity="warning",
                        category="home",
                        priority_hint=2,
                        module="home",
                        raw_payload={"room": room_id, "temp_c": temp, "threshold": threshold_low},
                        tags=["home", "temperature", room_id],
                        fingerprint=Candidate.make_fingerprint(
                            candidate_type="home_environment_alert",
                            source="home_sensor",
                            module="home",
                            title="room_temp_low",
                            raw_payload={"room": room_id, "threshold": threshold_low},
                        ),
                    ))

        # 4. HA source unavailable
        source = home.get("source")
        if source == "unavailable":
            candidates.append(Candidate(
                candidate_id=Candidate.make_id(),
                tick_id=tick_id,
                created_at=now,
                source="home_sensor",
                candidate_type="home_environment_alert",
                title="Home Assistant data unavailable",
                message="Home module cannot reach Home Assistant for sensor data.",
                severity="warning",
                category="system",
                priority_hint=2,
                module="home",
                raw_payload={"source": source},
                tags=["home", "system", "availability"],
                fingerprint=Candidate.make_fingerprint(
                    candidate_type="home_environment_alert",
                    source="home_sensor",
                    module="home",
                    title="ha_unavailable",
                    raw_payload={"source": source},
                ),
            ))

        return candidates


class ModuleHealthCandidateSource(CandidateSource):
    """Generate candidates from ModuleHealthTracker state.

    Includes per-module daily dedup using intelligence_state.json so the same
    stale/degraded/failed alert fires max once per calendar day (MDT), even
    though the engine ticks every 5 minutes.  Without this, a module that
    remains in a non-healthy state for hours would generate a new candidate
    every tick, flooding Matrix.
    """

    _DEDUP_PATH = Path.home() / ".hermes" / "helios" / "data" / "module_health_alert_dedup.json"

    def enabled(self, cfg: PriorityConfig) -> bool:
        return cfg.sources.module_health

    @classmethod
    def _load_dedup(cls) -> dict[str, Any]:
        try:
            if cls._DEDUP_PATH.exists():
                return json.loads(cls._DEDUP_PATH.read_text())
        except Exception:
            pass
        return {}

    @classmethod
    def _save_dedup(cls, state: dict[str, Any]) -> None:
        try:
            cls._DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
            cls._DEDUP_PATH.write_text(json.dumps(state, indent=2))
        except Exception:
            pass

    def generate(
        self,
        tick_id: str,
        context: dict[str, Any],
        rule_hits: list[dict],
        source_events: list[dict],
    ) -> list[Candidate]:
        now = datetime.now(timezone.utc).isoformat()
        today = datetime.now(
            __import__("zoneinfo").ZoneInfo("America/Edmonton")
        ).strftime("%Y-%m-%d")
        dedup = self._load_dedup()
        candidates: list[Candidate] = []

        # Module health is stored in engine context as "_module_health" (if present)
        # or we can try to read from health tracker via context if injected
        health = context.get("_module_health") or context.get("module_health", {})
        if not isinstance(health, dict):
            return candidates

        for module, state in health.items():
            if not isinstance(state, dict):
                continue
            mod_state = state.get("state", "unknown")
            if mod_state in ("healthy", "unknown"):
                continue

            # Daily dedup: one alert per module-state per day
            dedup_key = f"mh_alert_{module}_{mod_state}_{today}"
            if dedup.get(dedup_key):
                continue

            if mod_state == "stale":
                candidates.append(Candidate(
                    candidate_id=Candidate.make_id(),
                    tick_id=tick_id,
                    created_at=now,
                    source="module_health",
                    candidate_type="module_health_alert",
                    title=f"{module} data is stale",
                    message=f"{module} has not provided fresh data for {state.get('freshness_secs', 'unknown')}s.",
                    severity="warning",
                    category="system",
                    priority_hint=2,
                    module=module,
                    raw_payload=dict(state),
                    tags=["system", "health", "stale"],
                    fingerprint=Candidate.make_fingerprint(
                        candidate_type="module_health_alert",
                        source="module_health",
                        module=module,
                        title="module_stale",
                        raw_payload=dict(state),
                    ),
                ))
                dedup[dedup_key] = True
            elif mod_state in ("degraded", "failed"):
                candidates.append(Candidate(
                    candidate_id=Candidate.make_id(),
                    tick_id=tick_id,
                    created_at=now,
                    source="module_health",
                    candidate_type="module_health_alert",
                    title=f"{module} is {mod_state}",
                    message=f"{module} health state: {mod_state}. Consecutive failures: {state.get('consecutive_failures', 0)}",
                    severity="error" if mod_state == "failed" else "warning",
                    category="system",
                    priority_hint=3 if mod_state == "failed" else 2,
                    module=module,
                    raw_payload=dict(state),
                    tags=["system", "health", mod_state],
                    fingerprint=Candidate.make_fingerprint(
                        candidate_type="module_health_alert",
                        source="module_health",
                        module=module,
                        title=f"module_{mod_state}",
                        raw_payload=dict(state),
                    ),
                ))
                dedup[dedup_key] = True

        # Persist dedup state so we don't re-alert the same condition today
        if dedup:
            self._save_dedup(dedup)
        return candidates
