"""Priority Engine summarizer — daily/periodic rollup of candidate decisions.

Produces human-readable summaries of what the Priority Engine observed,
scored, selected, deferred, and suppressed over a time window.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("helios.priority.summarizer")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data" / "priority_engine"
SUMMARY_DIR = DATA_DIR / "summaries"


class PrioritySummarizer:
    """Query priority pipeline history and produce summary exports."""

    def __init__(self, db: Any):
        self.db = db
        SUMMARY_DIR.mkdir(parents=True, exist_ok=True)

    # ── public API ───────────────────────────────────────────────────

    def generate(self, hours: int = 24) -> dict[str, Any]:
        """Build a summary dict for the last N hours."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        candidates = self._query_candidates(since)
        scores = self._query_scores(since)
        decisions = self._query_decisions(since)

        # Link them
        score_map = {s["candidate_id"]: s for s in scores}
        cand_map = {c["candidate_id"]: c for c in candidates}

        enriched_decisions = []
        for d in decisions:
            cid = d["candidate_id"]
            enriched_decisions.append({
                **d,
                "title": cand_map.get(cid, {}).get("title", "Unknown"),
                "category": cand_map.get(cid, {}).get("category", "unknown"),
                "severity": cand_map.get(cid, {}).get("severity", "info"),
                "score": score_map.get(cid, {}).get("final_score", 0.0),
                "explanation": score_map.get(cid, {}).get("explanation", ""),
            })

        # Aggregate stats
        total_generated = len(candidates)
        total_scored = len(scores)
        total_decisions = len(decisions)

        selected = [d for d in enriched_decisions if d["decision"].startswith("select_")]
        suppressed = [d for d in enriched_decisions if d["decision"].startswith("suppress_")]
        deferred = [d for d in enriched_decisions if d["decision"] == "defer"]
        logged = [d for d in enriched_decisions if d["decision"] == "log_only"]

        # Category breakdown
        cat_counts: dict[str, int] = {}
        for c in candidates:
            cat = c.get("category", "unknown")
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

        # Decision breakdown by category
        cat_decisions: dict[str, dict[str, int]] = {}
        for d in enriched_decisions:
            cat = d.get("category", "unknown")
            dec = d["decision"]
            if cat not in cat_decisions:
                cat_decisions[cat] = {}
            cat_decisions[cat][dec] = cat_decisions[cat].get(dec, 0) + 1

        # Top candidates by score
        top_candidates = sorted(
            enriched_decisions,
            key=lambda x: x["score"],
            reverse=True,
        )[:10]

        # Score stats
        all_scores = [s["final_score"] for s in scores if s["final_score"] is not None]
        score_stats = {
            "count": len(all_scores),
            "avg": round(sum(all_scores) / len(all_scores), 3) if all_scores else 0.0,
            "max": round(max(all_scores), 3) if all_scores else 0.0,
            "min": round(min(all_scores), 3) if all_scores else 0.0,
        }

        summary = {
            "window_hours": hours,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "totals": {
                "generated": total_generated,
                "scored": total_scored,
                "decided": total_decisions,
                "selected": len(selected),
                "suppressed": len(suppressed),
                "deferred": len(deferred),
                "logged": len(logged),
            },
            "categories": cat_counts,
            "decisions_by_category": cat_decisions,
            "score_stats": score_stats,
            "top_candidates": [
                {
                    "candidate_id": d["candidate_id"],
                    "title": d["title"],
                    "category": d["category"],
                    "severity": d["severity"],
                    "score": d["score"],
                    "decision": d["decision"],
                    "reason": d.get("reason", ""),
                    "explanation": d["explanation"],
                }
                for d in top_candidates
            ],
            "suppressed_reasons": [
                {"title": d["title"], "reason": d.get("reason", ""), "score": d["score"]}
                for d in suppressed
            ],
            "deferred_reasons": [
                {"title": d["title"], "reason": d.get("reason", ""), "score": d["score"]}
                for d in deferred
            ],
        }

        return summary

    def write(self, summary: dict[str, Any], tag: str | None = None) -> Path:
        """Write summary to JSON and Markdown files."""
        tag = tag or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        json_path = SUMMARY_DIR / f"summary_{tag}.json"
        md_path = SUMMARY_DIR / f"summary_{tag}.md"

        # JSON
        try:
            with json_path.open("w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2, default=str)
        except Exception as exc:
            log.warning("Summary JSON write failed: %s", exc)

        # Markdown
        try:
            lines = self._to_markdown(summary)
            md_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception as exc:
            log.warning("Summary Markdown write failed: %s", exc)

        # Always overwrite latest_summary.json for quick CLI access
        latest_path = DATA_DIR / "latest_summary.json"
        try:
            with latest_path.open("w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2, default=str)
        except Exception as exc:
            log.warning("Latest summary write failed: %s", exc)

        return json_path

    def matrix_embed(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Build a compact embed dict for Matrix delivery (via _embed_to_html)."""
        totals = summary["totals"]
        top = summary["top_candidates"][:5]

        desc_lines = [
            f"Generated: **{totals['generated']}** | Scored: **{totals['scored']}** | "
            f"Selected: **{totals['selected']}** | Suppressed: **{totals['suppressed']}** | "
            f"Deferred: **{totals['deferred']}**",
            "",
            "**Top Candidates:**",
        ]
        for item in top:
            emoji = {
                "critical": "🔴", "error": "🟠", "warning": "🟡",
                "info": "🔵", "debug": "⚪",
            }.get(item["severity"], "⚪")
            desc_lines.append(
                f"{emoji} **{item['title']}** — `{item['decision']}` "
                f"(score: {item['score']:.2f})"
            )

        if summary["suppressed_reasons"]:
            desc_lines.extend(["", "**Suppressed:**"])
            for s in summary["suppressed_reasons"][:3]:
                desc_lines.append(f"• {s['title']} — {s['reason']}")

        embed = {
            "title": f"📊 Priority Engine Summary — Last {summary['window_hours']}h",
            "description": "\n".join(desc_lines),
            "color": 0x3498db,
            "footer": {
                "text": f"Generated at {summary['generated_at'][:19]} UTC",
            },
        }
        return embed

    # ── internal helpers ─────────────────────────────────────────────

    def _query_candidates(self, since_iso: str) -> list[dict]:
        sql = """
            SELECT candidate_id, title, category, severity, candidate_type, source, created_at
            FROM priority_candidates
            WHERE created_at >= ?
            ORDER BY created_at DESC
        """
        return self._query_all(sql, (since_iso,))

    def _query_scores(self, since_iso: str) -> list[dict]:
        sql = """
            SELECT candidate_id, final_score, explanation, urgency, importance, relevance,
                   confidence, context_fit, actionability, novelty, safety,
                   disruption_cost, staleness, annoyance, redundancy
            FROM priority_scores
            WHERE created_at >= ?
            ORDER BY final_score DESC
        """
        return self._query_all(sql, (since_iso,))

    def _query_decisions(self, since_iso: str) -> list[dict]:
        sql = """
            SELECT candidate_id, decision, route, reason, final_score, mode, created_at
            FROM priority_decisions
            WHERE created_at >= ?
            ORDER BY created_at DESC
        """
        return self._query_all(sql, (since_iso,))

    def _query_all(self, sql: str, params: tuple) -> list[dict]:
        try:
            with self.db._conn() as c:
                c.row_factory = sqlite3.Row
                rows = c.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("Summarizer query failed: %s", exc)
            return []

    def _to_markdown(self, summary: dict[str, Any]) -> list[str]:
        t = summary["totals"]
        lines = [
            f"# Priority Engine Summary — Last {summary['window_hours']}h",
            "",
            f"**Generated:** {summary['generated_at'][:19]} UTC",
            "",
            "## Totals",
            "",
            f"| Metric | Count |",
            f"|--------|-------|",
            f"| Generated | {t['generated']} |",
            f"| Scored | {t['scored']} |",
            f"| Decided | {t['decided']} |",
            f"| Selected | {t['selected']} |",
            f"| Suppressed | {t['suppressed']} |",
            f"| Deferred | {t['deferred']} |",
            f"| Logged | {t['logged']} |",
            "",
            "## Categories",
            "",
        ]
        for cat, count in sorted(summary["categories"].items(), key=lambda x: -x[1]):
            lines.append(f"- **{cat}**: {count}")

        lines.extend(["", "## Top Candidates", ""])
        lines.append("| Score | Decision | Severity | Category | Title | Reason |")
        lines.append("|------:|----------|----------|----------|-------|--------|")
        for item in summary["top_candidates"]:
            lines.append(
                f"| {item['score']:.2f} | {item['decision']} | {item['severity']} | "
                f"{item['category']} | {item['title']} | {item.get('reason', '')} |"
            )

        if summary["suppressed_reasons"]:
            lines.extend(["", "## Suppressed", ""])
            for s in summary["suppressed_reasons"]:
                lines.append(f"- **{s['title']}** — {s['reason']} (score: {s['score']:.2f})")

        if summary["deferred_reasons"]:
            lines.extend(["", "## Deferred", ""])
            for d in summary["deferred_reasons"]:
                lines.append(f"- **{d['title']}** — {d['reason']} (score: {d['score']:.2f})")

        lines.extend(["", "## Score Statistics", ""])
        ss = summary["score_stats"]
        lines.append(f"- Count: {ss['count']} | Avg: {ss['avg']} | Max: {ss['max']} | Min: {ss['min']}")

        return lines
