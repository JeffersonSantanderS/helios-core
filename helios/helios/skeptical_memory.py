"""
Helios v6 — Skeptical Memory Manager.

Phase 2 implementation. During dream cycles, audits Hermes memory
and memory.md for contradictions, assigns confidence scores,
and prunes low-confidence/stale entries.

Principles:
  1. 200-line hard cap on persistent memory
  2. Every fact must have a confidence score (0.0-1.0)
  3. Contradictions flagged and auto-resolved (newer wins)
  4. Age penalty: confidence decays 0.05/month for unverified facts
  5. No secrets in memory — flag and suggest rotation
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("helios.skeptical_memory")

MEMORY_MD = Path.home() / ".hermes" / "memory.md"
MAX_LINES = 200
SECRET_PATTERNS = [
    # Only flag actual exposed values, not file-path references
    (r'\b(?:api[_-]?key|secret|password|pass)\s*[:=]\s*(?!(?:~|/|\.\.|\$)\S+)\S{8,}', "exposed credential (not a path)"),
    (r'\b[a-z]{2,4}_[a-zA-Z0-9]{24,}\b', "environment variable key"),
    (r'\b[0-9a-f]{40,}\b', "hex key/token (40+ chars)"),
]


class Fact:
    """A single memory fact with confidence tracking."""

    def __init__(self, text: str, source: str, confidence: float = 0.7):
        self.text = text.strip()
        self.source = source
        self.confidence = confidence
        self.first_seen = time.time()
        self.last_verified = time.time()
        self.contradicted_by: list[str] = []
        self.secrets_detected: list[str] = []

    def age_days(self) -> float:
        return (time.time() - self.last_verified) / 86400.0

    def decay_confidence(self) -> float:
        """Apply age penalty: -0.05 per month unverified."""
        months = self.age_days() / 30.0
        penalty = months * 0.05
        return max(0.1, self.confidence - penalty)

    def scan_secrets(self) -> list[str]:
        """Detect exposed secrets."""
        hits = []
        for pattern, label in SECRET_PATTERNS:
            if re.search(pattern, self.text, re.IGNORECASE):
                hits.append(label)
        self.secrets_detected = hits
        return hits

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "source": self.source,
            "confidence": round(self.decay_confidence(), 2),
            "age_days": round(self.age_days(), 1),
            "contradicted": len(self.contradicted_by) > 0,
            "secrets": self.secrets_detected,
        }


class SkepticalMemory:
    """Audits, scores, and prunes persistent memory."""

    def __init__(self, db=None):
        self.db = db
        self.facts: list[Fact] = []
        self.audit_log: list[dict] = []

    # ── Parsing ─────────────────────────────────────────────────────────────

    def parse_memory_md(self) -> list[Fact]:
        """Parse ~/.hermes/memory.md into scorable facts."""
        facts = []
        if not MEMORY_MD.exists():
            return facts

        content = MEMORY_MD.read_text()
        current_section = "general"

        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                current_section = stripped.lstrip("# ").strip()
                continue

            # Extract fact text (strip list markers)
            fact_text = re.sub(r'^[-*]\s*', '', stripped)
            if len(fact_text) < 10:  # too short to be meaningful
                continue

            fact = Fact(fact_text, source=f"memory.md#{current_section}")
            fact.scan_secrets()
            facts.append(fact)

        return facts

    def parse_hermes_memory(self, entries: list[dict]) -> list[Fact]:
        """Convert Hermes memory entries to Facts."""
        facts = []
        for i, entry in enumerate(entries):
            text = entry.get("content", "")
            if not text or len(text) < 10:
                continue
            # Split multi-paragraph entries by § separator
            for paragraph in text.split("§"):
                para = paragraph.strip()
                if para and len(para) > 10:
                    fact = Fact(
                        para.strip(),
                        source=f"hermes_memory#{i+1}",
                        confidence=0.8,  # Hermes memory = higher baseline
                    )
                    fact.scan_secrets()
                    facts.append(fact)
        return facts

    # ── Audit ───────────────────────────────────────────────────────────────

    # Known truths — baseline facts the contradiction engine measures against.
    KNOWN_TRUTHS = {
        "location": "Home Assistant companion app (iPhone) — iCloud Find My as fallback",
        "tick_interval": "300 seconds (5 minutes)",
        "health_source": "iOS Health Auto Export → health-api",
        "spotify_tracking": "15-second polling with progress_ms",
        "dream_engine": "Phases 1-3 live, Phase 4 pending",
        "kairos": "purged May 2026 — Helios stands alone",
        "helios_domain": "Helios exclusive domain — no cross-agent delegation",
    }

    KNOWN_CONTRADICTIONS = [
        ("owntracks", "icloud find my", "OwnTracks retired; location is Home Assistant companion app (iCloud fallback)"),
        ("sparkyfitness", "health auto export", "SparkyFitness retired April 2026; use Health Auto Export"),
        ("fitness.module", "health auto export", "Fitness module retired; use health-api"),
        ("5 min.*tick", "300s", "Tick interval is 300s (5 min)"),
        ("kairos", "helios v6", "KAIROS purged; Helios v6 is the current system"),
        ("gemini", "searxng", "Gemini fallback is likely stale; SearXNG is primary"),
        ("icloud.*primary", "home assistant.*location", "Location: HA companion app is primary, iCloud is fallback only"),
        ("find my.*primary", "ha companion", "Find My demoted to fallback May 2026; HA is primary location source"),
    ]

    def audit(self, hermes_entries: Optional[list[dict]] = None) -> dict:
        """Full audit: parse, score, detect contradictions, identify prunes."""
        self.facts = self.parse_memory_md()
        if hermes_entries:
            self.facts.extend(self.parse_hermes_memory(hermes_entries))

        result = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "total_facts": len(self.facts),
            "secrets_found": 0,
            "contradictions": [],
            "low_confidence": [],
            "prune_candidates": [],
            "recommendations": [],
        }

        # Scan for secrets
        for fact in self.facts:
            if fact.secrets_detected:
                result["secrets_found"] += 1
                result["recommendations"].append(
                    f"⚠️  Secret found in {fact.source}: {', '.join(fact.secrets_detected)}"
                )

        # Detect contradictions using fuzzy matching
        for i, f1 in enumerate(self.facts):
            for j, f2 in enumerate(self.facts):
                if j <= i:
                    continue
                if self._contradict(f1, f2):
                    older = f1 if f1.first_seen < f2.first_seen else f2
                    newer = f2 if f1.first_seen < f2.first_seen else f1
                    c = {
                        "older": {"text": older.text[:80], "source": older.source},
                        "newer": {"text": newer.text[:80], "source": newer.source},
                    }
                    result["contradictions"].append(c)
                    older.contradicted_by.append(newer.text)
                    result["recommendations"].append(
                        f"⚡ Contradiction: [{older.source}] vs [{newer.source}] — prefer newer"
                    )

        # Low confidence (decayed below 0.3)
        for fact in self.facts:
            conf = fact.decay_confidence()
            if conf < 0.3:
                result["low_confidence"].append({
                    "text": fact.text[:80],
                    "source": fact.source,
                    "confidence": conf,
                })
                result["prune_candidates"].append(fact)

        # Contradicted facts get auto-flagged for prune
        for fact in self.facts:
            if fact.contradicted_by and fact not in result["prune_candidates"]:
                result["prune_candidates"].append(fact)

        # 200-line cap enforcement
        if len(self.facts) > MAX_LINES:
            # Prune lowest confidence first
            sorted_facts = sorted(self.facts, key=lambda f: f.decay_confidence())
            overflow = sorted_facts[:len(self.facts) - MAX_LINES]
            for fact in overflow:
                if fact not in result["prune_candidates"]:
                    result["prune_candidates"].append(fact)
            result["recommendations"].append(
                f"📏 {len(overflow)} facts exceed {MAX_LINES}-line cap — pruning lowest confidence"
            )

        self.audit_log.append(result)
        return result

    def _contradict(self, f1: Fact, f2: Fact) -> bool:
        """Detect if two facts contradict each other (simple heuristic)."""
        t1, t2 = f1.text.lower(), f2.text.lower()

        # ── Skip: clarifying statements are not contradictions ──
        # Facts that acknowledge a transition/retirement complement, not contradict.
        clarifying_markers = [
            r'\breplaced\s+by\b', r'\bretired\b', r'\bdeprecated\b',
            r'\bpurged\b', r'\bremoved\b', r'\bno longer\b',
        ]
        for marker in clarifying_markers:
            if re.search(marker, t1) or re.search(marker, t2):
                # If either fact explicitly acknowledges the state change,
                # they're complementary — not contradictory.
                return False

        # Contradiction signals
        contradiction_pairs = [
            (r'\bowntracks\b', r'\bicloud\b.*\bfind\b'),
            (r'\bprimary\b.*\blocation\b', r'\bicloud\b.*\blocation\b'),
            (r'\b5\s*min', r'\b300\s*s\b'),
            (r'\bretired\b', r'\bactive\b'),
            (r'\breplaced\b', r'\bcurrent\b'),
        ]

        for pattern_a, pattern_b in contradiction_pairs:
            a_in_1 = bool(re.search(pattern_a, t1))
            b_in_1 = bool(re.search(pattern_a, t2))
            a_in_2 = bool(re.search(pattern_b, t1))
            b_in_2 = bool(re.search(pattern_b, t2))

            # Both facts mention the same topic but in conflicting ways
            if (a_in_1 and b_in_2) or (a_in_2 and b_in_1):
                return True

        # Same source, different values
        if f1.source == f2.source:
            # Extract key-value patterns like "sleep.hours = X"
            kv1 = re.findall(r'(\w+\.?\w*)\s*[:=]\s*(\S+)', t1)
            kv2 = re.findall(r'(\w+\.?\w*)\s*[:=]\s*(\S+)', t2)
            for (k1, v1), (k2, v2) in zip(kv1, kv2):
                if k1 == k2 and v1 != v2:
                    return True

        return False

    # ── Prune execution ────────────────────────────────────────────────────

    def execute_prune(self, audit: dict) -> dict:
        """Prune flagged facts from memory.md and log actions."""
        result = {"pruned": 0, "errors": 0, "actions": []}

        candidates = audit.get("prune_candidates", [])
        if not candidates:
            return result

        # For memory.md, rewrite the file minus pruned lines
        prune_texts = {c.text for c in candidates if c.source.startswith("memory.md")}

        if prune_texts and MEMORY_MD.exists():
            content = MEMORY_MD.read_text()
            new_lines = []
            for line in content.splitlines():
                stripped = re.sub(r'^[-*]\s*', '', line.strip())
                if stripped in prune_texts:
                    result["actions"].append(f"Pruned: {stripped[:60]}")
                    result["pruned"] += 1
                    continue
                new_lines.append(line)
            MEMORY_MD.write_text("\n".join(new_lines) + "\n")

        # For Hermes memory, log recommendations (can't modify directly)
        hermes_prunes = [c for c in candidates if c.source.startswith("hermes_memory")]
        for c in hermes_prunes:
            result["actions"].append(
                f"Recommend prune Hermes memory: {c.text[:60]} (conf={c.decay_confidence():.2f})"
            )
            result["pruned"] += 1

        log.info("Skeptical memory: pruned %d facts (%d errors)",
                 result["pruned"], result["errors"])
        return result

    # ── Summary for dream cycle ────────────────────────────────────────────

    def get_dream_summary(self, audit: dict) -> list[str]:
        """Return bullet points for the dream engine summary."""
        items = []
        if audit["contradictions"]:
            items.append(f"{len(audit['contradictions'])} memory contradictions resolved")
        if audit["secrets_found"]:
            items.append(f"⚠️ {audit['secrets_found']} secrets exposed in memory")
        if audit["low_confidence"]:
            items.append(f"{len(audit['low_confidence'])} low-confidence facts flagged")
        if audit["prune_candidates"]:
            items.append(f"{len(audit['prune_candidates'])} facts pruned/flagged")
        if audit["total_facts"] > MAX_LINES:
            items.append(f"Memory at {audit['total_facts']} lines (>200 cap)")
        return items
