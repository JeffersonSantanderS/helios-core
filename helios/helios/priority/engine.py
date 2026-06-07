"""Priority Engine — main orchestrator."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .models import Candidate, CandidateScore, CandidateDecision, PriorityResult
from .config import PriorityConfig
from .builder import CandidateBuilder, RuleHitCandidateSource, HomeCandidateSource, ModuleHealthCandidateSource
from .hydrators import CandidateHydrator
from .filters import CandidateFilters
from .scorers import CandidateScorer
from .selector import CandidateSelector
from .logger import PriorityLogger
from .exports import PriorityExporter

log = logging.getLogger("helios.priority.engine")


class PriorityEngine:
    """Deterministic candidate ranking and decision layer for Helios."""

    def __init__(self, db: Any, cfg: Any = None, preferences: Any = None, health: Any = None):
        self.db = db
        self.cfg = PriorityConfig.from_raw(cfg or {})
        self.preferences = preferences
        self.health = health

        self.builder = CandidateBuilder(self.cfg)
        self.builder.register(RuleHitCandidateSource())
        self.builder.register(HomeCandidateSource())
        self.builder.register(ModuleHealthCandidateSource())

        self.hydrator = CandidateHydrator(db, self.cfg, preferences, health)
        self.filters = CandidateFilters(self.cfg)
        self.scorer = CandidateScorer(self.cfg)
        self.selector = CandidateSelector(self.cfg)
        self.logger = PriorityLogger(db)
        self.exporter = PriorityExporter(db, self.cfg)

    def evaluate_tick(
        self,
        context: dict[str, Any],
        rule_hits: list[dict] | None = None,
        source_events: list[dict] | None = None,
        mode: str | None = None,
    ) -> PriorityResult:
        """Run full priority pipeline for one tick."""
        tick_id = datetime.now(timezone.utc).isoformat()
        effective_mode = mode or self.cfg.mode

        if not self.cfg.enabled:
            return PriorityResult(
                tick_id=tick_id,
                mode=effective_mode,
                generated_count=0,
                filtered_count=0,
                scored_count=0,
                selected_count=0,
                suppressed_count=0,
                deferred_count=0,
                candidates=[],
                decisions=[],
                summary={"status": "disabled"},
            )

        try:
            # 1. Generate candidates
            candidates = self.builder.from_tick(
                tick_id=tick_id,
                context=context,
                rule_hits=rule_hits or [],
                source_events=source_events or [],
            )
            generated = len(candidates)

            # 2. Hydrate
            hydrated = [self.hydrator.hydrate(c, context) for c in candidates]

            # 3. Filter
            filtered = self.filters.apply(hydrated, context)
            filtered_count = len(filtered)

            # 4. Score
            scored_list = [self.scorer.score(c, context) for c in filtered]
            scored_count = len(scored_list)

            # 5. Select
            decisions = self.selector.select(scored_list, filtered, mode=effective_mode)
            selected = len([d for d in decisions if d.decision.startswith("select_")])
            suppressed = len([d for d in decisions if d.decision.startswith("suppress_")])
            deferred = len([d for d in decisions if d.decision == "defer"])

            # 6. Log
            self.logger.log_all(tick_id, filtered, scored_list, decisions)

            # 7. Export
            self.exporter.write_latest(tick_id, effective_mode, filtered, scored_list, decisions)

            return PriorityResult(
                tick_id=tick_id,
                mode=effective_mode,
                generated_count=generated,
                filtered_count=generated - filtered_count,
                scored_count=scored_count,
                selected_count=selected,
                suppressed_count=suppressed,
                deferred_count=deferred,
                candidates=filtered,
                decisions=decisions,
                summary={
                    "status": "ok",
                    "sources_used": [c.source for c in filtered],
                    "top_score": scored_list[0].final_score if scored_list else 0.0,
                },
            )

        except Exception as exc:
            log.warning("PriorityEngine failed: %s", exc)
            return PriorityResult(
                tick_id=tick_id,
                mode=effective_mode,
                generated_count=0,
                filtered_count=0,
                scored_count=0,
                selected_count=0,
                suppressed_count=0,
                deferred_count=0,
                candidates=[],
                decisions=[],
                summary={"status": "error"},
                error=str(exc),
            )

    def get_suppressed_rule_slugs(self, result: PriorityResult) -> set[str]:
        """Return set of rule slugs that were suppressed by the selector.

        Used by HeliosEngine for Phase 4 soft control: skip dispatch
        for rule hits whose candidates were suppressed as duplicate or
        low-score (but never critical or user-requested).
        """
        suppressed: set[str] = set()
        for dec in result.decisions:
            if dec.decision.startswith("suppress_"):
                # Find the candidate to get its rule_slug
                for cand in result.candidates:
                    if cand.candidate_id == dec.candidate_id:
                        if cand.rule_slug:
                            suppressed.add(cand.rule_slug)
                        break
        return suppressed
