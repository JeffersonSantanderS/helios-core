"""Helios v6 — autoDream Memory Consolidation Engine.

This engine reviews the day's data during idle windows,
detects real patterns across modules, prunes stale data, and produces concise summaries.

Three-gate trigger:
  1. IDLE: AFK >= idle_threshold (default 600s)
  2. ACCUMULATED: new metric_snapshots rows since last dream cycle
  3. NO ACTIVE TASKS: (deferred — no task system yet)

Four-phase consolidation:
  A. Extraction — pull today's metrics, focus, mood from DB
  B. Pattern Detection — compare against 7d/30d averages, cross-module hints
  C. Linking — connect observations across modules
  D. Pruning — delete rows older than retention periods

Skeptical Memory (Phase 2):
  After consolidation, audits persistent memory for contradictions,
  exposed secrets, and low-confidence facts. Enforces 200-line cap.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .skeptical_memory import SkepticalMemory
from .proactive_intelligence import ProactiveIntelligence

log = logging.getLogger("helios.dream_engine")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
DREAM_STATE_FILE = DATA_DIR / "dream_state.json"
IDLE_STATE_FILE = DATA_DIR / "idle_state.json"

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "idle_threshold_secs": 600,       # 10 min AFK
    "dream_interval_secs": 3600,      # min time between dream cycles
    "retention_metric_days": 90,      # keep metric_snapshots for 90 days
    "retention_focus_days": 30,       # keep focus rows for 30 days
    "anomaly_stddev": 2.0,            # z-score threshold for anomaly
    "anomaly_min_baseline": 7,        # need at least 7 days for baseline
}


class DreamEngine:
    """Performs memory consolidation during idle periods."""

    def __init__(self, db, cfg: Optional[dict] = None):
        self.db = db
        self.cfg = {**DEFAULT_CONFIG, **(cfg or {}).get("dream_engine", {})}
        self._last_dream_ts: Optional[float] = None
        self._last_metric_count: int = 0
        self._last_auto_dream_date: Optional[str] = None
        self.skeptical = SkepticalMemory(db)
        self.proactive = ProactiveIntelligence(db)
        self._load_state()

    # ── State persistence ─────────────────────────────────────────────────────

    def _load_state(self) -> None:
        """Restore dream engine state from disk."""
        if DREAM_STATE_FILE.exists():
            try:
                data = json.loads(DREAM_STATE_FILE.read_text())
                self._last_dream_ts = data.get("last_dream_ts")
                self._last_metric_count = data.get("last_metric_count", 0)
                self._last_auto_dream_date = data.get("last_auto_dream_date")
            except Exception:
                pass

    def _save_state(self) -> None:
        """Persist dream engine state to disk."""
        try:
            DREAM_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            DREAM_STATE_FILE.write_text(json.dumps({
                "last_dream_ts": self._last_dream_ts,
                "last_metric_count": self._last_metric_count,
                "last_auto_dream_date": self._last_auto_dream_date,
            }))
        except Exception:
            pass

    # ── Gate checks ───────────────────────────────────────────────────────────

    def is_idle_window(self) -> bool:
        """Check if local time falls in the automatic night dream window (01:00-06:00 MDT)."""
        local_tz = ZoneInfo("America/Edmonton")
        now_local = datetime.now(local_tz)

        # Night window: 01:00-06:00 local time
        if 1 <= now_local.hour < 6:
            idle_secs = 0
            if IDLE_STATE_FILE.exists():
                try:
                    data = json.loads(IDLE_STATE_FILE.read_text())
                    idle_secs = data.get("idle_seconds", 0)
                except Exception:
                    pass
            threshold = self.cfg["idle_threshold_secs"]
            if idle_secs >= threshold:
                log.info("Dream gate (idle): AFK %ds >= %ds (night window %d:00 MDT)",
                         idle_secs, threshold, now_local.hour)
                return True
            log.info("Dream gate (idle): night window but only %ds AFK < %ds threshold",
                     idle_secs, threshold)
            return False

        log.debug("Dream gate (idle): outside night window (%d:00 MDT)", now_local.hour)
        return False

    def has_new_data(self) -> bool:
        """Check if new metric data has accumulated since last dream."""
        conn = self.db._conn()
        cur = conn.execute("SELECT COUNT(*) FROM metric_snapshots")
        current = cur.fetchone()[0]

        if current > self._last_metric_count:
            delta = current - self._last_metric_count
            log.info("Dream gate (data): %d new metrics since last dream", delta)
            self._last_metric_count = current
            return True
        return False

    def should_dream(self) -> bool:
        """Return True if all gates pass, interval cooldown is clear, and daily cap not exceeded."""
        # Gate 1: idle (night window + AFK)
        if not self.is_idle_window():
            return False

        # Gate 2: new data
        if not self.has_new_data():
            return False

        # Gate 3: daily cap — max 1 automatic dream per local calendar day
        local_tz = ZoneInfo("America/Edmonton")
        today_local = datetime.now(local_tz).strftime("%Y-%m-%d")
        if self._last_auto_dream_date == today_local:
            log.info("Dream gate (daily cap): already dreamed today (%s)", today_local)
            return False

        # Cooldown: don't dream more than once per interval
        interval = self.cfg["dream_interval_secs"]
        if self._last_dream_ts and (time.time() - self._last_dream_ts) < interval:
            return False

        return True

    # ── Full dream cycle ──────────────────────────────────────────────────────

    def run_dream_cycle(self) -> dict[str, Any]:
        """Execute a full dream cycle: extract -> pattern -> link -> prune."""
        self._last_dream_ts = time.time()
        self._last_auto_dream_date = datetime.now(ZoneInfo("America/Edmonton")).strftime("%Y-%m-%d")
        self._save_state()
        ts = datetime.now(timezone.utc).isoformat()
        log.info("🧠 Dream cycle starting at %s", ts)

        result = {
            "ts": ts,
            "extraction": {},
            "patterns": [],
            "links": [],
            "pruned": {},
            "summary": "",
            "anomalies": [],
            "memory_audit": {},
            "proactive": {},
        }

        try:
            result["extraction"] = self._extract()
            result["patterns"] = self._find_patterns(result["extraction"])
            result["links"] = self._link(result["extraction"], result["patterns"])
            result["anomalies"] = self._detect_anomalies(result["extraction"])
            result["pruned"] = self._prune()
            # Phase 2: Skeptical Memory audit
            result["memory_audit"] = self.skeptical.audit()
            memory_prune = self.skeptical.execute_prune(result["memory_audit"])
            result["pruned"]["memory_facts"] = memory_prune["pruned"]
            # Phase 3: Proactive Intelligence
            result["proactive"] = self.proactive.dream_analysis()
            result["summary"] = self._summarize(result)
        except Exception as exc:
            log.warning("Dream cycle error: %s", exc, exc_info=True)
            result["error"] = str(exc)

        self._persist_result(result)
        log.info("🧠 Dream complete: %d metrics, %d patterns, %d anomalies",
                 result["extraction"].get("today_metrics", 0),
                 len(result["patterns"]),
                 len(result["anomalies"]))
        return result

    # ── Phase A: Extraction ───────────────────────────────────────────────────

    def _extract(self) -> dict:
        """Pull today's data from all live sources."""
        conn = self.db._conn()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Today's metric_snapshots
        cur = conn.execute(
            "SELECT metric, value, source FROM metric_snapshots WHERE date_key=?",
            (today,)
        )
        today_metrics = {}
        for row in cur.fetchall():
            today_metrics[row[0]] = {"value": row[1], "source": row[2]}

        # 7-day and 30-day averages for each metric
        averages = {}
        for window_days, label in [(7, "avg_7d"), (30, "avg_30d")]:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
            cur = conn.execute(
                "SELECT metric, AVG(value), COUNT(DISTINCT date_key) "
                "FROM metric_snapshots WHERE date_key >= ? GROUP BY metric",
                (cutoff,)
            )
            for row in cur.fetchall():
                if row[0] not in averages:
                    averages[row[0]] = {}
                averages[row[0]][label] = round(row[1], 2)
                averages[row[0]][f"n_{label[-2:]}"] = row[2]

        # Today's focus breakdown (aggregate by state)
        cur = conn.execute(
            "SELECT state, COUNT(*) as cnt, SUM(duration_secs) as total_secs "
            "FROM focus WHERE ts LIKE ? || '%' GROUP BY state",
            (today,)
        )
        focus_today = {}
        for row in cur.fetchall():
            focus_today[row[0]] = {"occurrences": row[1], "total_seconds": row[2] or 0}

        # Latest mood
        cur = conn.execute(
            "SELECT score, emoji, note FROM mood ORDER BY ts DESC LIMIT 1"
        )
        mood_row = cur.fetchone()
        latest_mood = None
        if mood_row:
            latest_mood = {"score": mood_row[0], "emoji": mood_row[1], "note": mood_row[2]}

        # Significant correlations (strong, p < 0.01)
        cur = conn.execute(
            "SELECT metric_a, metric_b, window_days, pearson_r, strength, direction "
            "FROM correlations WHERE strength='strong' AND p_value < 0.01 "
            "ORDER BY ABS(pearson_r) DESC LIMIT 10"
        )
        correlations = [dict(zip(
            ["metric_a", "metric_b", "window_days", "pearson_r", "strength", "direction"],
            row
        )) for row in cur.fetchall()]

        return {
            "date": today,
            "today_metrics": len(today_metrics),
            "metrics": today_metrics,
            "averages": averages,
            "focus": focus_today,
            "mood": latest_mood,
            "correlations": correlations,
        }

    # ── Phase B: Pattern Detection ────────────────────────────────────────────

    def _find_patterns(self, extraction: dict) -> list[dict]:
        """Detect meaningful patterns from today's data vs. historical norms."""
        patterns: list[dict] = []
        metrics = extraction.get("metrics", {})
        averages = extraction.get("averages", {})
        focus = extraction.get("focus", {})

        # Check each today metric against 7-day average
        for metric, info in sorted(metrics.items()):
            value = info.get("value")
            if value is None:
                continue
            avg_data = averages.get(metric, {})
            avg_7d = avg_data.get("avg_7d")
            n_7d = avg_data.get("n_7d", 0)

            if avg_7d and n_7d >= 3 and avg_7d > 0:
                delta_pct = round(((value - avg_7d) / avg_7d) * 100, 1)
                if abs(delta_pct) >= 30:
                    direction = "↑" if delta_pct > 0 else "↓"
                    patterns.append({
                        "type": "metric_delta",
                        "metric": metric,
                        "today": value,
                        "avg_7d": avg_7d,
                        "delta_pct": delta_pct,
                        "direction": direction,
                        "significance": "high" if abs(delta_pct) >= 50 else "medium",
                    })

        # Focus patterns
        if focus:
            total_focus_secs = sum(f.get("total_seconds", 0) for f in focus.values())
            if total_focus_secs > 0:
                for state, info in sorted(focus.items(), key=lambda x: -x[1].get("total_seconds", 0)):
                    pct = round((info.get("total_seconds", 0) / max(total_focus_secs, 1)) * 100, 1)
                    if pct >= 10:
                        patterns.append({
                            "type": "focus_breakdown",
                            "state": state,
                            "seconds": info.get("total_seconds", 0),
                            "pct": pct,
                        })

        # Correlation-driven patterns
        for corr in extraction.get("correlations", []):
            a, b = corr.get("metric_a", ""), corr.get("metric_b", "")
            if a in metrics and b in metrics:
                patterns.append({
                    "type": "correlation_active",
                    "pair": f"{a}↔{b}",
                    "pearson_r": corr.get("pearson_r"),
                    "window_days": corr.get("window_days"),
                })

        return patterns

    # ── Phase C: Linking ──────────────────────────────────────────────────────

    def _link(self, extraction: dict, patterns: list[dict]) -> list[dict]:
        """Cross-reference patterns to form multi-module insights."""
        links: list[dict] = []
        metrics = extraction.get("metrics", {})

        # Sleep + Mood link
        sleep = metrics.get("sleep.hours", {}).get("value")
        mood = metrics.get("mood.score_daily", {}).get("value")
        if sleep is not None and mood is not None:
            if sleep < 5 and mood >= 7:
                links.append({
                    "type": "sleep_mood_resilience",
                    "detail": f"Only {sleep}h sleep but mood is {mood}/10 — you're running on fumes well"
                })
            elif sleep < 5 and mood <= 4:
                links.append({
                    "type": "sleep_mood_crash",
                    "detail": f"{sleep}h sleep, mood {mood}/10 — sleep debt is hitting hard"
                })

        # Spotify + Sleep link
        spotify_min = metrics.get("spotify.listen_minutes_daily", {}).get("value")
        spotify_tracks = metrics.get("spotify.tracks_daily", {}).get("value")
        if spotify_min is not None and sleep is not None:
            if spotify_min > 120 and sleep < 5:
                links.append({
                    "type": "spotify_late_night",
                    "detail": f"{spotify_min:.0f} min Spotify ({spotify_tracks} tracks) + only {sleep}h sleep — late night listening?"
                })

        # Activity + Heart rate link
        activity = metrics.get("activity.minutes_daily", {}).get("value")
        hr = metrics.get("resting_heart_rate.avg_daily", {}).get("value")
        if activity is not None and hr is not None:
            if activity < 10 and hr > 70:
                links.append({
                    "type": "sedentary_hr",
                    "detail": f"Low activity ({activity}min) + elevated RHR ({hr} bpm) — stress or recovery?"
                })

        # Gaming spike
        focus = extraction.get("focus", {})
        gaming = focus.get("gaming", {})
        if gaming:
            gaming_secs = gaming.get("total_seconds", 0)
            if gaming_secs > 7200:  # 2 hours
                hrs = round(gaming_secs / 3600, 1)
                links.append({
                    "type": "gaming_marathon",
                    "detail": f"{hrs}h gaming today — how's the rest of your day looking?"
                })

        return links

    # ── Anomaly Detection ─────────────────────────────────────────────────────

    def _detect_anomalies(self, extraction: dict) -> list[dict]:
        """Flag metrics that deviate significantly from longer-term baselines."""
        anomalies: list[dict] = []
        metrics = extraction.get("metrics", {})
        averages = extraction.get("averages", {})
        threshold = self.cfg["anomaly_stddev"]
        min_baseline = self.cfg["anomaly_min_baseline"]
        today = extraction.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

        # We need stddev, not just avg. Query it directly.
        conn = self.db._conn()

        for metric, info in metrics.items():
            value = info.get("value")
            if value is None:
                continue
            avg_data = averages.get(metric, {})
            n = avg_data.get("n_30d", 0)
            if n < min_baseline:
                continue

            cur = conn.execute(
                "SELECT AVG(value), COUNT(DISTINCT date_key) FROM metric_snapshots "
                "WHERE metric=? AND date_key >= ? AND date_key != ?",
                (metric,
                 (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"),
                 today)
            )
            row = cur.fetchone()
            if not row or row[0] is None or row[1] < min_baseline:
                continue

            baseline_avg = row[0]
            if baseline_avg == 0:
                continue

            # Calculate stddev
            cur = conn.execute(
                "SELECT value FROM metric_snapshots "
                "WHERE metric=? AND date_key >= ? AND date_key != ?",
                (metric,
                 (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"),
                 today)
            )
            vals = [r[0] for r in cur.fetchall() if r[0] is not None]
            if len(vals) < min_baseline:
                continue

            mean = sum(vals) / len(vals)
            variance = sum((v - mean) ** 2 for v in vals) / len(vals)
            stddev = variance ** 0.5
            if stddev == 0:
                continue

            z_score = abs((value - mean) / stddev)
            if z_score >= threshold:
                direction = "above" if value > mean else "below"
                anomalies.append({
                    "metric": metric,
                    "today": value,
                    "baseline_mean": round(mean, 2),
                    "stddev": round(stddev, 2),
                    "z_score": round(z_score, 2),
                    "direction": direction,
                })

        return anomalies

    # ── Phase D: Pruning ──────────────────────────────────────────────────────

    def _prune(self) -> dict[str, int]:
        """Delete rows older than retention periods."""
        conn = self.db._conn()
        results: dict[str, int] = {}

        # metric_snapshots
        metric_cutoff = (datetime.now(timezone.utc) -
                         timedelta(days=self.cfg["retention_metric_days"])).strftime("%Y-%m-%d")
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM metric_snapshots WHERE date_key < ?", (metric_cutoff,)
            )
            metric_count = cur.fetchone()[0]
            if metric_count > 0:
                conn.execute("DELETE FROM metric_snapshots WHERE date_key < ?", (metric_cutoff,))
                conn.commit()
                results["metric_snapshots"] = metric_count
                log.info("Pruned %d metric_snapshots rows (older than %s)", metric_count, metric_cutoff)
        except Exception as exc:
            log.warning("Metric prune failed: %s", exc)

        # focus
        focus_cutoff = (datetime.now(timezone.utc) -
                        timedelta(days=self.cfg["retention_focus_days"])).isoformat()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM focus WHERE ts < ?", (focus_cutoff,)
            )
            focus_count = cur.fetchone()[0]
            if focus_count > 0:
                conn.execute("DELETE FROM focus WHERE ts < ?", (focus_cutoff,))
                conn.commit()
                results["focus"] = focus_count
                log.info("Pruned %d focus rows (older than %d days)", focus_count,
                         self.cfg["retention_focus_days"])
        except Exception as exc:
            log.warning("Focus prune failed: %s", exc)

        # Idle JSONL files: delete older than 14 days
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=14)
            removed = 0
            for fpath in DATA_DIR.glob("idle_*.jsonl"):
                try:
                    mtime = datetime.fromtimestamp(fpath.stat().st_mtime, tz=timezone.utc)
                    if mtime < cutoff:
                        fpath.unlink()
                        removed += 1
                except Exception:
                    pass
            if removed:
                results["idle_jsonl"] = removed
                log.info("Pruned %d idle JSONL files (older than %d days)", removed, 14)
        except Exception as exc:
            log.warning("Idle JSONL prune failed: %s", exc)

        return results

    # ── Summarization ─────────────────────────────────────────────────────────

    def _summarize(self, result: dict) -> str:
        """Generate a concise human-readable summary."""
        ext = result.get("extraction", {})
        today = ext.get("date", datetime.now().strftime("%Y-%m-%d"))
        metrics = ext.get("metrics", {})
        patterns = result.get("patterns", [])
        links = result.get("links", [])
        anomalies = result.get("anomalies", [])

        lines = [f"# Dream Recap — {today}", ""]

        # Core stats
        sleep = metrics.get("sleep.hours", {}).get("value", "?")
        mood = metrics.get("mood.score_daily", {}).get("value", "?")
        activity = metrics.get("activity.minutes_daily", {}).get("value", "?")
        spotify = metrics.get("spotify.listen_minutes_daily", {}).get("value", 0)

        lines.append(f"Sleep: {sleep}h | Activity: {activity}min | Mood: {mood}/10 | Spotify: {spotify:.0f}min")
        lines.append("")

        # Anomalies first — most important
        if anomalies:
            lines.append("## Anomalies")
            for a in anomalies[:5]:
                arrow = "▲" if a["direction"] == "above" else "▼"
                lines.append(f"- {arrow} **{a['metric']}**: {a['today']} (z={a['z_score']}, baseline={a['baseline_mean']})")
            lines.append("")

        # Patterns
        if patterns:
            lines.append("## Patterns")
            metric_deltas = [p for p in patterns if p["type"] == "metric_delta"]
            for p in metric_deltas[:5]:
                lines.append(
                    f"- {p['direction']} **{p['metric']}**: {p['today']} "
                    f"(avg {p['avg_7d']} → {p['delta_pct']:+.1f}%)"
                )

            focus_patterns = [p for p in patterns if p["type"] == "focus_breakdown"]
            if focus_patterns:
                lines.append("- Focus: " + " | ".join(
                    f"{fp['state']} {fp['pct']:.0f}%" for fp in focus_patterns[:4]
                ))
            lines.append("")

        # Cross-module links
        if links:
            lines.append("## Insights")
            for link in links[:5]:
                lines.append(f"- {link['detail']}")
            lines.append("")

        # Pruning
        pruned = result.get("pruned", {})
        if pruned:
            pruned_items = [f"{k}={v}" for k, v in pruned.items() if v > 0]
            if pruned_items:
                lines.append(f"Pruned: {', '.join(pruned_items)}")

        # Skeptical Memory audit
        memory = result.get("memory_audit", {})
        if memory.get("contradictions"):
            lines.append(f"Memory: {len(memory['contradictions'])} contradictions resolved")
        if memory.get("secrets_found", 0) > 0:
            lines.append(f"Memory: ⚠️ {memory['secrets_found']} secrets found")
        if memory.get("low_confidence"):
            lines.append(f"Memory: {len(memory['low_confidence'])} low-confidence facts")

        lines.append("")
        lines.append("---")
        lines.append("*Dream cycle by Helios v6*")

        return "\n".join(lines)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist_result(self, result: dict) -> None:
        """Save dream cycle result to DB and state file."""
        # Save state
        self._save_state()

        # Log to DB decisions
        try:
            self.db.insert_decision(
                decision_type="dream_cycle",
                source="dream_engine",
                action="autoDream",
                context={
                    "patterns": len(result.get("patterns", [])),
                    "anomalies": len(result.get("anomalies", [])),
                    "links": len(result.get("links", [])),
                },
                module="dream_engine",
            )
        except Exception as exc:
            log.debug("Failed to persist dream decision: %s", exc)

    # ── Discord push ──────────────────────────────────────────────────────────

    def get_idle_summary(self) -> Optional[str]:
        """Return the summary for Discord push (called by engine)."""
        rows = self.db.get_decisions_by_type("dream_cycle", limit=1)
        if not rows:
            return None

        # Return the summary directly from the last dream cycle result
        # We just ran it, so we can reconstruct
        try:
            latest = dict(rows[0])
            ctx = latest.get("context", {})
            if isinstance(ctx, str):
                ctx = json.loads(ctx)
            pattern_count = ctx.get("patterns", 0)
            anomaly_count = ctx.get("anomalies", 0)
            if pattern_count == 0 and anomaly_count == 0:
                return None
            # The full summary was composed in _summarize and returned
            # But it's not stored in the DB decision. Let's return a compact version.
            if anomaly_count > 0:
                return f"Dream cycle: {pattern_count} patterns, {anomaly_count} anomalies found. Check dream_state.json."
            return f"Dream cycle: {pattern_count} patterns detected."
        except Exception:
            return None
