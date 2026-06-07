"""Candidate selector — choose top candidates after scoring."""

from __future__ import annotations

from typing import Any

from .models import Candidate, CandidateScore, CandidateDecision
from .config import PriorityConfig
from .filters import CandidateFilters


class CandidateSelector:
    """Select candidates for dispatch based on scores, thresholds, and diversity."""

    def __init__(self, cfg: PriorityConfig):
        self.cfg = cfg
        self.filters = CandidateFilters(cfg)

    def select(self, scores: list[CandidateScore], candidates: list[Candidate], mode: str) -> list[CandidateDecision]:
        """Return decisions for all candidates."""
        # Build lookup
        cand_by_id = {c.candidate_id: c for c in candidates}
        score_by_id = {s.candidate_id: s for s in scores}
        th = self.cfg.thresholds
        sel_cfg = self.cfg.selector

        # Sort by final score descending
        sorted_scores = sorted(scores, key=lambda s: s.final_score, reverse=True)

        selected_count = 0
        notify_count = 0
        dm_count = 0
        category_counts: dict[str, int] = {}
        decisions: list[CandidateDecision] = []
        seen_categories: set[str] = set()

        for score in sorted_scores:
            c = cand_by_id.get(score.candidate_id)
            if c is None:
                continue

            decision, route, reason, execute = self._decide(
                c, score, th, sel_cfg, selected_count, notify_count, dm_count,
                category_counts, mode, seen_categories,
            )

            decisions.append(CandidateDecision(
                candidate_id=c.candidate_id,
                decision=decision,
                route=route,
                reason=reason,
                final_score=score.final_score,
                threshold_used=self._threshold_for(decision, th),
                execute_now=execute,
                mode=mode,
            ))

            # Track counts only for selected/notify/dm
            if decision in ("select_notify", "select_dm", "select_channel"):
                selected_count += 1
                if decision == "select_notify":
                    notify_count += 1
                if decision == "select_dm":
                    dm_count += 1
                category_counts[c.category] = category_counts.get(c.category, 0) + 1
                seen_categories.add(c.category)

        # Second pass: apply cross-candidate redundancy on home_environment alerts
        # If multiple candidates target the same room, downgrade weaker ones
        decisions = self._apply_room_redundancy(decisions, candidates, score_by_id, mode)

        return decisions

    def _apply_room_redundancy(
        self,
        decisions: list[CandidateDecision],
        candidates: list[Candidate],
        score_by_id: dict[str, CandidateScore],
        mode: str,
    ) -> list[CandidateDecision]:
        """If multiple home_environment alerts target the same room, keep only the highest-scoring."""
        room_best: dict[str, tuple[str, float]] = {}  # room -> (candidate_id, score)

        # Find best per room
        for c in candidates:
            if c.candidate_type != "home_environment_alert":
                continue
            room_tags = [t for t in c.tags if t in ("master_bedroom", "spare_bedroom", "living_room")]
            if not room_tags:
                continue
            sc = score_by_id.get(c.candidate_id)
            if sc is None:
                continue
            for room in room_tags:
                best_id, best_score = room_best.get(room, (None, 0.0))
                if best_id is None or sc.final_score > best_score:
                    room_best[room] = (c.candidate_id, sc.final_score)

        # Mark non-best room candidates as deferred
        updated: list[CandidateDecision] = []
        seen_rooms: dict[str, str] = {}  # room -> kept candidate_id
        for dec in decisions:
            c = next((c for c in candidates if c.candidate_id == dec.candidate_id), None)
            if c is None or c.candidate_type != "home_environment_alert":
                updated.append(dec)
                continue
            room_tags = [t for t in c.tags if t in ("master_bedroom", "spare_bedroom", "living_room")]
            if not room_tags:
                updated.append(dec)
                continue
            best_for_room = None
            for room in room_tags:
                best_id, _ = room_best.get(room, (None, 0.0))
                if best_id:
                    best_for_room = best_id
                    break
            if best_for_room and best_for_room != c.candidate_id:
                # Downgrade to summary or log-only
                updated.append(CandidateDecision(
                    candidate_id=c.candidate_id,
                    decision="defer",
                    route="deferred",
                    reason=f"Redundant: stronger candidate for same room",
                    final_score=dec.final_score,
                    threshold_used=dec.threshold_used,
                    execute_now=False,
                    mode=mode,
                ))
            else:
                updated.append(dec)
        return updated

    def _decide(
        self,
        c: Candidate,
        score: CandidateScore,
        th: Any,
        sel_cfg: Any,
        selected_count: int,
        notify_count: int,
        dm_count: int,
        category_counts: dict[str, int],
        mode: str,
        seen_categories: set[str],
    ) -> tuple[str, str, str, bool]:
        # Never suppress critical in Phase 1
        if self.filters.is_critical(c) and self.cfg.safeguards.never_suppress_critical:
            if score.final_score >= th.notify:
                return "select_notify", "matrix_channel", "Critical severity — always notify", True
            return "select_log_only", "log", "Critical but low score, logging for review", True

        # Never suppress user-requested
        if self.filters.is_user_requested(c) and self.cfg.safeguards.never_block_user_requested_reminders:
            return "select_dm", "matrix_dm", "User-requested reminder", True

        # Max limits
        if selected_count >= sel_cfg.max_selected_per_tick:
            return "defer", "deferred", f"Max selected per tick ({sel_cfg.max_selected_per_tick}) reached", False

        if category_counts.get(c.category, 0) >= sel_cfg.max_per_category_per_tick:
            return "defer", "deferred", f"Max per category ({sel_cfg.max_per_category_per_tick}) reached", False

        # Route by score
        if score.final_score >= th.dm:
            if dm_count >= sel_cfg.max_dm_per_tick:
                return "select_notify", "matrix_channel", f"Score {score.final_score:.2f} >= DM threshold but DM limit reached", False
            return "select_dm", "matrix_dm", f"Score {score.final_score:.2f} >= DM threshold", False

        if score.final_score >= th.notify:
            if notify_count >= sel_cfg.max_notify_per_tick:
                return "select_summary", "summary", f"Score {score.final_score:.2f} >= notify but notify limit reached", False
            return "select_notify", "matrix_channel", f"Score {score.final_score:.2f} >= notify threshold", False

        if score.final_score >= th.summary:
            return "select_summary", "summary", f"Score {score.final_score:.2f} >= summary threshold", False

        if score.final_score >= th.log_only:
            return "select_log_only", "log", f"Score {score.final_score:.2f} >= log-only threshold", False

        # Low score suppression
        if score.novelty < 0.1 and not self.filters.is_critical(c):
            return "suppress_duplicate", "suppressed", f"Very low novelty ({score.novelty:.2f}), likely duplicate", False

        if score.annoyance > 0.6:
            return "suppress_duplicate", "suppressed", f"High annoyance ({score.annoyance:.2f}), too repetitive", False

        return "suppress_low_score", "suppressed", f"Score {score.final_score:.2f} below all thresholds", False

    def _threshold_for(self, decision: str, th: Any) -> float:
        return {
            "select_dm": th.dm,
            "select_notify": th.notify,
            "select_summary": th.summary,
            "select_log_only": th.log_only,
        }.get(decision, 0.0)
