"""Priority Engine debug exports — JSON and Markdown."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .models import Candidate, CandidateScore, CandidateDecision

log = logging.getLogger("helios.priority.exports")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data" / "priority_engine"


class PriorityExporter:
    """Write latest priority engine state to JSON and Markdown."""

    def __init__(self, db: Any, cfg: Any = None):
        self.db = db
        self.cfg = cfg
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def write_latest(
        self,
        tick_id: str,
        mode: str,
        candidates: list[Candidate],
        scores: list[CandidateScore],
        decisions: list[CandidateDecision],
    ) -> None:
        if not (self.cfg and getattr(self.cfg, "export_debug", True)):
            return

        # Build score/decision lookup
        score_map = {s.candidate_id: s for s in scores}
        dec_map = {d.candidate_id: d for d in decisions}

        top = []
        for c in candidates:
            s = score_map.get(c.candidate_id)
            d = dec_map.get(c.candidate_id)
            if s and d:
                top.append({
                    "candidate_id": c.candidate_id,
                    "title": c.title,
                    "type": c.candidate_type,
                    "category": c.category,
                    "severity": c.severity,
                    "score": s.final_score,
                    "decision": d.decision,
                    "route": d.route,
                    "reason": d.reason,
                    "explanation": s.explanation,
                })

        top.sort(key=lambda x: x["score"], reverse=True)

        payload = {
            "tick_id": tick_id,
            "mode": mode,
            "generated": len(candidates),
            "scored": len(scores),
            "selected": len([d for d in decisions if d.decision.startswith("select_")]),
            "suppressed": len([d for d in decisions if d.decision.startswith("suppress_")]),
            "deferred": len([d for d in decisions if d.decision == "defer"]),
            "top_candidates": top[:10],
        }

        # JSON export
        json_path = DATA_DIR / "latest.json"
        try:
            with json_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
        except Exception as exc:
            log.warning("PriorityExporter JSON failed: %s", exc)

        # Markdown export
        md_path = DATA_DIR / "latest.md"
        try:
            lines = [
                "# Priority Engine — Latest Tick",
                "",
                f"**Tick ID:** {tick_id}  ",
                f"**Mode:** {mode}  ",
                f"**Generated:** {payload['generated']}  ",
                f"**Scored:** {payload['scored']}  ",
                f"**Selected:** {payload['selected']}  ",
                f"**Suppressed:** {payload['suppressed']}  ",
                f"**Deferred:** {payload['deferred']}",
                "",
                "## Top Candidates",
                "",
                "| Score | Decision | Type | Category | Title | Reason |",
                "|------:|----------|------|----------|-------|--------|",
            ]
            for item in top:
                lines.append(
                    f"| {item['score']:.2f} | {item['decision']} | {item['type']} | "
                    f"{item['category']} | {item['title']} | {item['reason']} |"
                )
            lines.extend(["", "## Details", ""])
            for item in top:
                lines.append(f"### {item['title']} ({item['score']:.2f})")
                lines.append("")
                lines.append(f"- **Decision:** {item['decision']}")
                lines.append(f"- **Route:** {item['route']}")
                lines.append(f"- **Explanation:** {item['explanation']}")
                lines.append("")

            md_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception as exc:
            log.warning("PriorityExporter Markdown failed: %s", exc)
