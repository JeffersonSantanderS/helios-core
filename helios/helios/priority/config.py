"""Priority Engine configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SelectorConfig:
    max_selected_per_tick: int = 5
    max_notify_per_tick: int = 2
    max_dm_per_tick: int = 1
    max_per_category_per_tick: int = 1


@dataclass
class ScoringWeights:
    urgency: float = 1.35
    importance: float = 1.25
    relevance: float = 1.10
    confidence: float = 1.00
    context_fit: float = 0.95
    actionability: float = 0.90
    novelty: float = 0.50
    safety: float = 1.50
    disruption_cost: float = 1.20
    staleness: float = 0.80
    annoyance: float = 1.10
    redundancy: float = 1.15


@dataclass
class ScoringThresholds:
    dm: float = 0.86
    notify: float = 0.70
    summary: float = 0.45
    log_only: float = 0.20


@dataclass
class SourceConfig:
    rules: bool = True
    home: bool = True
    module_health: bool = True
    predictive: bool = False
    proactive: bool = False
    daily_intelligence: bool = False
    llm_requests: bool = False


@dataclass
class SafeguardConfig:
    never_suppress_critical: bool = True
    never_block_self_healing: bool = True
    never_block_user_requested_reminders: bool = True
    quiet_hours_penalty: float = 0.25
    driving_penalty: float = 0.35
    meeting_penalty: float = 0.30


@dataclass
class PriorityConfig:
    enabled: bool = True
    mode: str = "shadow"
    fail_open: bool = True
    log_all_candidates: bool = True
    export_debug: bool = True

    selector: SelectorConfig = field(default_factory=SelectorConfig)
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    thresholds: ScoringThresholds = field(default_factory=ScoringThresholds)
    sources: SourceConfig = field(default_factory=SourceConfig)
    safeguards: SafeguardConfig = field(default_factory=SafeguardConfig)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "PriorityConfig":
        cfg = cls()
        if not raw:
            return cfg

        cfg.enabled = raw.get("enabled", True)
        cfg.mode = raw.get("mode", "shadow")
        cfg.fail_open = raw.get("fail_open", True)
        cfg.log_all_candidates = raw.get("log_all_candidates", True)
        cfg.export_debug = raw.get("export_debug", True)

        sel = raw.get("selector", {})
        cfg.selector = SelectorConfig(
            max_selected_per_tick=sel.get("max_selected_per_tick", 5),
            max_notify_per_tick=sel.get("max_notify_per_tick", 2),
            max_dm_per_tick=sel.get("max_dm_per_tick", 1),
            max_per_category_per_tick=sel.get("max_per_category_per_tick", 1),
        )

        sw = raw.get("scoring", {}).get("weights", {})
        cfg.weights = ScoringWeights(
            urgency=sw.get("urgency", 1.35),
            importance=sw.get("importance", 1.25),
            relevance=sw.get("relevance", 1.10),
            confidence=sw.get("confidence", 1.00),
            context_fit=sw.get("context_fit", 0.95),
            actionability=sw.get("actionability", 0.90),
            novelty=sw.get("novelty", 0.50),
            safety=sw.get("safety", 1.50),
            disruption_cost=sw.get("disruption_cost", 1.20),
            staleness=sw.get("staleness", 0.80),
            annoyance=sw.get("annoyance", 1.10),
            redundancy=sw.get("redundancy", 1.15),
        )

        st = raw.get("scoring", {}).get("thresholds", {})
        cfg.thresholds = ScoringThresholds(
            dm=st.get("dm", 0.86),
            notify=st.get("notify", 0.70),
            summary=st.get("summary", 0.45),
            log_only=st.get("log_only", 0.20),
        )

        src = raw.get("sources", {})
        cfg.sources = SourceConfig(
            rules=src.get("rules", True),
            home=src.get("home", True),
            module_health=src.get("module_health", True),
            predictive=src.get("predictive", False),
            proactive=src.get("proactive", False),
            daily_intelligence=src.get("daily_intelligence", False),
            llm_requests=src.get("llm_requests", False),
        )

        safe = raw.get("safeguards", {})
        cfg.safeguards = SafeguardConfig(
            never_suppress_critical=safe.get("never_suppress_critical", True),
            never_block_self_healing=safe.get("never_block_self_healing", True),
            never_block_user_requested_reminders=safe.get(
                "never_block_user_requested_reminders", True
            ),
            quiet_hours_penalty=safe.get("quiet_hours_penalty", 0.25),
            driving_penalty=safe.get("driving_penalty", 0.35),
            meeting_penalty=safe.get("meeting_penalty", 0.30),
        )

        return cfg

    def to_dict(self) -> dict[str, Any]:
        """Convert config to raw dict for PriorityEngine constructor."""
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "fail_open": self.fail_open,
            "log_all_candidates": self.log_all_candidates,
            "export_debug": self.export_debug,
            "selector": {
                "max_selected_per_tick": self.selector.max_selected_per_tick,
                "max_notify_per_tick": self.selector.max_notify_per_tick,
                "max_dm_per_tick": self.selector.max_dm_per_tick,
                "max_per_category_per_tick": self.selector.max_per_category_per_tick,
            },
            "scoring": {
                "weights": {
                    "urgency": self.weights.urgency,
                    "importance": self.weights.importance,
                    "relevance": self.weights.relevance,
                    "confidence": self.weights.confidence,
                    "context_fit": self.weights.context_fit,
                    "actionability": self.weights.actionability,
                    "novelty": self.weights.novelty,
                    "safety": self.weights.safety,
                    "disruption_cost": self.weights.disruption_cost,
                    "staleness": self.weights.staleness,
                    "annoyance": self.weights.annoyance,
                    "redundancy": self.weights.redundancy,
                },
                "thresholds": {
                    "dm": self.thresholds.dm,
                    "notify": self.thresholds.notify,
                    "summary": self.thresholds.summary,
                    "log_only": self.thresholds.log_only,
                },
            },
            "sources": {
                "rules": self.sources.rules,
                "home": self.sources.home,
                "module_health": self.sources.module_health,
                "predictive": self.sources.predictive,
                "proactive": self.sources.proactive,
                "daily_intelligence": self.sources.daily_intelligence,
                "llm_requests": self.sources.llm_requests,
            },
            "safeguards": {
                "never_suppress_critical": self.safeguards.never_suppress_critical,
                "never_block_self_healing": self.safeguards.never_block_self_healing,
                "never_block_user_requested_reminders": self.safeguards.never_block_user_requested_reminders,
                "quiet_hours_penalty": self.safeguards.quiet_hours_penalty,
                "driving_penalty": self.safeguards.driving_penalty,
                "meeting_penalty": self.safeguards.meeting_penalty,
            },
        }
