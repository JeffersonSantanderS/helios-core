"""Helios Brain v1 — Unified deterministic brain state export.

Live — wired into HeliosEngine.tick() after write_all_exports().
See: docs/HELIOS_BRAIN_V1.md for full documentation.

Collects existing module outputs (telemetry, rules, patterns, insights,
actions, health) into a single stable JSON contract that agents can consume
without guessing from scattered files.

Design principles:
  - Deterministic first. No LLM calls anywhere.
  - Aggregate, don't compute. Read from existing SQLite tables, JSON files,
    and in-memory module outputs. Never re-implement logic.
  - Evidence-linked. Every belief, rule status, and suggestion traces back
    to source data.
  - Confidence-aware. Stale or missing data is represented honestly,
    never silently accepted as valid.
  - Atomic write. Uses the same temp+rename pattern as stable_exports.
  - Graceful degradation. If a module raises or returns None, that section
    populates with safe defaults rather than crashing the export.

Consume via:
  ~/.hermes/helios/brain_state.json
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("helios.brain_state")

SCHEMA_VERSION = "1.0"
BRAIN_STATE_FILE = Path.home() / ".hermes" / "helios" / "brain_state.json"

# ── Freshness degradation thresholds (seconds) ──────────────────────────
_FRESH_FRESH = 300       # ≤5 min
_FRESH_STALE = 900       # ≤15 min
_FRESH_DEGRADED = 3600   # ≤1 hour
# beyond 3600 → stale/failed


# ═══════════════════════════════════════════════════════════════════════════
# Atomic write helper (mirrors stable_exports._write_json_atomic)
# ═══════════════════════════════════════════════════════════════════════════

def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_confidence(value: float) -> float:
    """Clamp a confidence value to [0.0, 1.0] and round to 3 decimals."""
    return round(max(0.0, min(1.0, value)), 3)


def _freshness_label(secs: float) -> str:
    """Convert freshness seconds to a human label."""
    if secs <= _FRESH_FRESH:
        return "fresh"
    if secs <= _FRESH_STALE:
        return "stale"
    if secs <= _FRESH_DEGRADED:
        return "degraded"
    return "failed"


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert to float, returning default on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ═══════════════════════════════════════════════════════════════════════════
# Brain State Builder
# ═══════════════════════════════════════════════════════════════════════════

class BrainStateBuilder:
    """Collects existing module outputs into a unified Brain v1 contract.

    Parameters
    ----------
    db_path : str
        Path to the Helios SQLite database.
    health : ModuleHealthTracker or None
        The engine's health tracker. Gracefully handled if None.
    preferences : PreferenceEngine or None
        The engine's preference/pattern engine. Gracefully handled if None.
    rules_engine : RulesEngine or None
        The engine's rules engine. Gracefully handled if None.
    config : dict or None
        Optional runtime config for context.
    """

    def __init__(
        self,
        db_path: str,
        health: Any = None,
        preferences: Any = None,
        rules_engine: Any = None,
        config: Any = None,
    ):
        self.db_path = db_path
        self.health = health
        self.preferences = preferences
        self.rules_engine = rules_engine
        self.config = config or {}

    # ── Public API ─────────────────────────────────────────────────────

    def build(self) -> dict[str, Any]:
        """Build the complete brain state dict.

        Never raises. Missing modules produce safe defaults.
        Stale data is tagged honestly.
        """
        state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now_iso(),
            "runtime": self._build_runtime(),
            "current_state": self._build_current_state(),
            "beliefs": self._build_beliefs(),
            "active_rules": self._build_active_rules(),
            "pattern_deviations": self._build_pattern_deviations(),
            "suggestions": self._build_suggestions(),
            "suppressed_alerts": self._build_suppressed_alerts(),
            "evidence_trace": self._build_evidence_trace(),
        }
        return state

    def export(self, path: Optional[Path] = None) -> Path:
        """Build and atomically write brain_state.json.

        Returns the written Path.
        """
        target = path or BRAIN_STATE_FILE
        data = self.build()
        _write_json_atomic(target, data)
        logger.info("brain_state.json written to %s", target)
        return target

    # ── Runtime section ─────────────────────────────────────────────────

    def _build_runtime(self) -> dict[str, Any]:
        """Daemon state, data freshness, overall confidence, module health."""
        module_health: dict[str, Any] = {}
        freshness_map: dict[str, float] = {}
        overall_confidence = 0.0

        if self.health is not None and hasattr(self.health, "summary"):
            try:
                summary = self.health.summary()
                for name, entry in summary.items():
                    confidence = entry.get("confidence", 0.0)
                    fresh_secs = entry.get("freshness_secs", 99999)
                    module_health[name] = {
                        "state": entry.get("state", "unknown"),
                        "confidence": _clamp_confidence(confidence),
                        "freshness_secs": round(fresh_secs, 1),
                        "freshness_label": _freshness_label(fresh_secs),
                        "last_ok_ts": entry.get("last_ok_ts"),
                    }
                    freshness_map[name] = fresh_secs

                # Weighted average confidence across modules
                if summary:
                    confidences = [
                        e.get("confidence", 0.0) for e in summary.values()
                    ]
                    overall_confidence = _clamp_confidence(
                        sum(confidences) / len(confidences)
                    )
            except Exception as exc:
                logger.debug("Health summary failed: %s", exc)

        return {
            "daemon_state": "running",
            "data_freshness": freshness_map,
            "overall_confidence": overall_confidence,
            "module_health": module_health,
        }

    # ── Current state section ───────────────────────────────────────────

    def _build_current_state(self) -> dict[str, Any]:
        """Current snapshot of key life dimensions from context/metrics.

        Returns 'unknown' for any dimension where data is stale or missing.
        """
        state: dict[str, str] = {
            "location": "unknown",
            "activity": "unknown",
            "focus": "unknown",
            "energy": "unknown",
            "calendar": "unknown",
            "health": "unknown",
            "mood": "unknown",
            "protein": "unknown",
            "system": "unknown",
        }

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            # Focus state
            try:
                row = conn.execute(
                    "SELECT state FROM focus ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if row:
                    state["focus"] = row["state"] or "unknown"
            except sqlite3.OperationalError:
                pass

            # Mood
            try:
                row = conn.execute(
                    "SELECT value FROM metric_snapshots "
                    "WHERE metric = 'mood.score_daily' AND date_key = ? LIMIT 1",
                    (today,),
                ).fetchone()
                if row:
                    state["mood"] = str(row["value"])
            except sqlite3.OperationalError:
                pass

            # Sleep (as energy proxy)
            try:
                row = conn.execute(
                    "SELECT value FROM metric_snapshots "
                    "WHERE metric = 'sleep.hours' AND date_key = ? LIMIT 1",
                    (today,),
                ).fetchone()
                if row:
                    hrs = _safe_float(row["value"])
                    if hrs >= 7:
                        state["energy"] = "well_rested"
                    elif hrs >= 5:
                        state["energy"] = "moderate"
                    elif hrs > 0:
                        state["energy"] = "low"
            except sqlite3.OperationalError:
                pass

            # Protein
            try:
                row = conn.execute(
                    "SELECT value FROM metric_snapshots "
                    "WHERE metric = 'protein.daily_total' AND date_key = ? LIMIT 1",
                    (today,),
                ).fetchone()
                if row:
                    state["protein"] = str(row["value"])
            except sqlite3.OperationalError:
                pass

            # Steps (activity proxy)
            try:
                row = conn.execute(
                    "SELECT value FROM metric_snapshots "
                    "WHERE metric = 'activity.steps_daily' AND date_key = ? LIMIT 1",
                    (today,),
                ).fetchone()
                if row:
                    steps = int(_safe_float(row["value"], 0))
                    if steps >= 8000:
                        state["activity"] = "active"
                    elif steps >= 3000:
                        state["activity"] = "moderate"
                    elif steps > 0:
                        state["activity"] = "sedentary"
            except sqlite3.OperationalError:
                pass

            # Calendar
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM calendar_events WHERE date_key = ?",
                    (today,),
                ).fetchone()
                if row:
                    cnt = row["cnt"]
                    state["calendar"] = f"{cnt}_events" if cnt > 0 else "clear"
            except sqlite3.OperationalError:
                pass

            # Location from context
            try:
                row = conn.execute(
                    "SELECT value FROM context WHERE module = 'home' "
                    "AND key = 'home.presence' ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if row:
                    try:
                        val = json.loads(row["value"])
                        if isinstance(val, dict):
                            state["location"] = val.get("location", "unknown")
                        else:
                            state["location"] = str(val)
                    except (json.JSONDecodeError, TypeError):
                        state["location"] = str(row["value"])
            except sqlite3.OperationalError:
                pass

            # Health summary
            try:
                row = conn.execute(
                    "SELECT value FROM metric_snapshots "
                    "WHERE metric = 'health.composite_score' AND date_key = ? LIMIT 1",
                    (today,),
                ).fetchone()
                if row:
                    state["health"] = str(row["value"])
            except sqlite3.OperationalError:
                pass

            conn.close()
        except Exception as exc:
            logger.debug("current_state build failed: %s", exc)

        # Mark dimensions as stale/degraded if health tracker says so
        if self.health is not None and hasattr(self.health, "freshness"):
            for dim, module_name in [
                ("focus", "focus"),
                ("mood", "mood"),
                ("energy", "sleep"),
                ("protein", "protein"),
                ("activity", "activity"),
                ("health", "health"),
            ]:
                try:
                    fresh_secs = self.health.freshness(module_name)
                    if fresh_secs > _FRESH_DEGRADED:
                        if state[dim] != "unknown":
                            state[dim] = f"{state[dim]}_stale"
                except Exception:
                    pass

        return state

    # ── Beliefs section ─────────────────────────────────────────────────

    def _build_beliefs(self) -> list[dict[str, Any]]:
        """Inferred beliefs from preference patterns + top correlations.

        Each belief has a key, value, confidence, sources, freshness, and evidence.
        """
        beliefs: list[dict[str, Any]] = []

        # ── From preference engine patterns ──
        if self.preferences is not None and hasattr(self.preferences, "all_patterns"):
            try:
                patterns = self.preferences.all_patterns()
                if isinstance(patterns, dict):
                    for key, pat in patterns.items():
                        if not isinstance(pat, dict):
                            continue
                        confidence = _clamp_confidence(pat.get("confidence", 0.5))
                        beliefs.append({
                            "key": f"preference.{key}",
                            "value": pat.get("value", pat.get("summary", "unknown")),
                            "confidence": confidence,
                            "sources": ["preference_engine"],
                            "freshness_seconds": pat.get("age_seconds", 99999),
                            "evidence": pat.get("evidence", []),
                        })
            except Exception as exc:
                logger.debug("Preferences beliefs failed: %s", exc)

        # ── From SQLite correlations ──
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT metric_a, metric_b, strength, direction, p_value, "
                "last_observed_at FROM correlations "
                "WHERE abs(strength) > 0.3 "
                "ORDER BY abs(strength) DESC LIMIT 10"
            ).fetchall()

            for r in rows:
                strength = _safe_float(r["strength"], 0)
                p_value = _safe_float(r["p_value"], 1.0)
                # Higher strength + lower p-value → higher confidence
                confidence = _clamp_confidence(abs(strength) * (1.0 - min(p_value, 1.0)))
                beliefs.append({
                    "key": f"correlation.{r['metric_a']}_{r['metric_b']}",
                    "value": f"{r['direction']}: r={strength:.2f}, p={p_value:.4f}",
                    "confidence": confidence,
                    "sources": ["correlator"],
                    "freshness_seconds": 99999,  # Correlations are weekly
                    "evidence": [f"correlations table id={r['metric_a']}_{r['metric_b']}"],
                })
            conn.close()
        except Exception as exc:
            logger.debug("Correlation beliefs failed: %s", exc)

        return beliefs

    # ── Active rules section ────────────────────────────────────────────

    def _build_active_rules(self) -> list[dict[str, Any]]:
        """Evaluated rule states from rules_v2.

        Includes status: triggered, suppressed, cooldown, inactive.
        """
        rules: list[dict[str, Any]] = []

        # ── From rules engine ──
        if self.rules_engine is not None and hasattr(self.rules_engine, "evaluate"):
            try:
                # Minimal context for evaluation — rules read from DB context
                context = {}
                hits = self.rules_engine.evaluate(context)
                for hit in hits:
                    slug = hit.get("slug", "unknown")
                    rules.append({
                        "rule_id": slug,
                        "status": "triggered",
                        "priority": hit.get("priority", "normal"),
                        "reason": hit.get("message", hit.get("reason", "")),
                        "confidence": _clamp_confidence(hit.get("confidence", 0.5)),
                    })
            except Exception as exc:
                logger.debug("Rules evaluation failed: %s", exc)

        # ── From DB: non-triggered enabled rules ──
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            triggered_slugs = {r["rule_id"] for r in rules}
            rows = conn.execute(
                "SELECT slug, priority, category, enabled, last_triggered "
                "FROM rules WHERE enabled = 1"
            ).fetchall()
            now = datetime.now(timezone.utc)

            for r in rows:
                slug = r["slug"]
                if slug in triggered_slugs:
                    continue  # Already listed as triggered

                status = "inactive"
                reason = "rule not triggered this tick"
                confidence = 0.5

                # Check cooldown
                if r["last_triggered"]:
                    try:
                        last_triggered = datetime.fromisoformat(
                            r["last_triggered"].replace("Z", "+00:00")
                        )
                        cooldown_secs = 3600  # Default 1h cooldown
                        elapsed = (now - last_triggered).total_seconds()
                        if elapsed < cooldown_secs:
                            status = "cooldown"
                            reason = f"in cooldown ({int(cooldown_secs - elapsed)}s remaining)"
                            confidence = _clamp_confidence(1.0 - (elapsed / cooldown_secs) * 0.3)
                    except Exception:
                        pass

                rules.append({
                    "rule_id": slug,
                    "status": status,
                    "priority": r["priority"] or "normal",
                    "reason": reason,
                    "confidence": _clamp_confidence(confidence),
                })

            conn.close()
        except Exception as exc:
            logger.debug("DB rules query failed: %s", exc)

        return rules

    # ── Pattern deviations section ──────────────────────────────────────

    def _build_pattern_deviations(self) -> list[dict[str, Any]]:
        """Deviations of current metrics from inferred baselines.

        Compares current values from metric_snapshots against the inferred
        patterns from PreferenceEngine.
        """
        deviations: list[dict[str, Any]] = []

        if self.preferences is None or not hasattr(self.preferences, "all_patterns"):
            return deviations

        try:
            patterns = self.preferences.all_patterns()
            if not isinstance(patterns, dict):
                return deviations

            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            for key, pat in patterns.items():
                if not isinstance(pat, dict):
                    continue
                # Look for stats-based patterns with mean/stddev
                baseline = pat.get("mean") or pat.get("baseline")
                if baseline is None:
                    continue

                # Find the current value for this metric
                metric_name = pat.get("metric") or key
                row = conn.execute(
                    "SELECT value FROM metric_snapshots "
                    "WHERE metric = ? AND date_key = ? LIMIT 1",
                    (metric_name, today),
                ).fetchone()

                if row is None:
                    continue

                current = _safe_float(row["value"])
                baseline_val = _safe_float(baseline)
                stddev = _safe_float(pat.get("stddev", 0))

                if stddev > 0:
                    z_score = (current - baseline_val) / stddev
                else:
                    z_score = 0.0 if current == baseline_val else 999.0

                # Only surface meaningful deviations (|z| > 1.5)
                if abs(z_score) > 1.5:
                    deviations.append({
                        "pattern": key,
                        "baseline": round(baseline_val, 2),
                        "current": round(current, 2),
                        "deviation": f"z={z_score:.2f}",
                        "confidence": _clamp_confidence(
                            min(1.0, abs(z_score) / 3.0)
                        ),
                        "sample_count": pat.get("sample_count", 0),
                    })

            conn.close()
        except Exception as exc:
            logger.debug("Pattern deviations failed: %s", exc)

        return deviations

    # ── Suggestions section ──────────────────────────────────────────────

    def _build_suggestions(self) -> list[dict[str, Any]]:
        """Actionable suggestions from priority dispatches, predictive alerts,
        and dream cycle outputs.

        Every suggestion includes requires_confirmation=True unless
        the source is a low-priority informational nudge.
        """
        suggestions: list[dict[str, Any]] = []

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            # ── From alert_history (recent dispatched alerts) ──
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            rows = conn.execute(
                "SELECT ts, rule_slug, severity, message, category "
                "FROM alert_history WHERE ts >= ? ORDER BY ts DESC LIMIT 20",
                (cutoff,),
            ).fetchall()

            for r in rows:
                severity = r["severity"] or "info"
                suggestions.append({
                    "id": f"alert_{r['rule_slug']}_{r['ts']}",
                    "type": "alert_dispatch",
                    "priority": severity,
                    "message": r["message"] or "",
                    "reason": f"Rule {r['rule_slug']} triggered",
                    "action_candidate": r["rule_slug"],
                    "requires_confirmation": severity in ("high", "critical"),
                })

            # ── From context (prediction results) ──
            try:
                pred_rows = conn.execute(
                    "SELECT key, value FROM context "
                    "WHERE module = 'predictor' ORDER BY ts DESC LIMIT 5"
                ).fetchall()
                for r in pred_rows:
                    try:
                        val = json.loads(r["value"]) if isinstance(r["value"], str) else r["value"]
                        suggestions.append({
                            "id": f"prediction_{r['key']}",
                            "type": "prediction",
                            "priority": "info",
                            "message": str(val),
                            "reason": f"Predictor output: {r['key']}",
                            "action_candidate": None,
                            "requires_confirmation": True,
                        })
                    except (json.JSONDecodeError, TypeError):
                        pass
            except sqlite3.OperationalError:
                pass

            conn.close()
        except Exception as exc:
            logger.debug("Suggestions build failed: %s", exc)

        return suggestions

    # ── Suppressed alerts section ────────────────────────────────────────

    def _build_suppressed_alerts(self) -> list[dict[str, Any]]:
        """Alerts that were intentionally held back.

        Sources: quiet hours, cooldown, low confidence,
        not interruptible, duplicate detection.
        """
        suppressed: list[dict[str, Any]] = []

        # ── Quiet hours ──
        if self.preferences is not None and hasattr(self.preferences, "is_quiet_hours"):
            try:
                if self.preferences.is_quiet_hours():
                    suppressed.append({
                        "rule_id": "*",
                        "reason": "quiet_hours",
                    })
            except Exception:
                pass

        # ── Stale modules (from health tracker) ──
        if self.health is not None and hasattr(self.health, "summary"):
            try:
                summary = self.health.summary()
                for name, entry in summary.items():
                    state = entry.get("state", "unknown")
                    if state in ("degraded", "failed", "anomalous"):
                        suppressed.append({
                            "rule_id": f"module_{name}",
                            "reason": f"low_confidence:{state}",
                        })
            except Exception:
                pass

        # ── Cooldown rules (from DB) ──
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            now = datetime.now(timezone.utc)
            rows = conn.execute(
                "SELECT slug, last_triggered FROM rules WHERE enabled = 1 AND last_triggered IS NOT NULL"
            ).fetchall()

            for r in rows:
                if r["last_triggered"]:
                    try:
                        last = datetime.fromisoformat(
                            r["last_triggered"].replace("Z", "+00:00")
                        )
                        elapsed = (now - last).total_seconds()
                        if elapsed < 3600:
                            suppressed.append({
                                "rule_id": r["slug"],
                                "reason": f"cooldown:{int(3600 - elapsed)}s_remaining",
                            })
                    except Exception:
                        pass

            conn.close()
        except Exception as exc:
            logger.debug("Suppressed alerts cooldown query failed: %s", exc)

        return suppressed

    # ── Evidence trace section ───────────────────────────────────────────

    def _build_evidence_trace(self) -> list[dict[str, Any]]:
        """Chain of custody for every data source used in this export.

        Includes: context entries, metric snapshots, rule evaluations,
        alert dispatches, correlation observations.
        """
        trace: list[dict[str, Any]] = []
        generated_at = _now_iso()

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            # ── Recent context entries ──
            cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            try:
                rows = conn.execute(
                    "SELECT source, module, key, ts FROM context WHERE ts >= ? ORDER BY ts DESC LIMIT 50",
                    (cutoff_24h,),
                ).fetchall()
                for r in rows:
                    trace.append({
                        "source": r["source"] or r["module"],
                        "source_id": f"context:{r['module']}:{r['key']}",
                        "timestamp": r["ts"],
                        "used_for": "current_state",
                    })
            except sqlite3.OperationalError:
                pass

            # ── Recent metric snapshots ──
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            try:
                rows = conn.execute(
                    "SELECT metric, source, date_key FROM metric_snapshots "
                    "WHERE date_key = ? LIMIT 30",
                    (today,),
                ).fetchall()
                for r in rows:
                    trace.append({
                        "source": r["source"] or "data_ingestion",
                        "source_id": f"metric:{r['metric']}",
                        "timestamp": generated_at,
                        "used_for": "current_state,beliefs",
                    })
            except sqlite3.OperationalError:
                pass

            # ── Rule evaluations ──
            try:
                rows = conn.execute(
                    "SELECT slug, last_triggered FROM rules WHERE enabled = 1 AND last_triggered >= ?",
                    (cutoff_24h,),
                ).fetchall()
                for r in rows:
                    trace.append({
                        "source": "rules_v2",
                        "source_id": f"rule:{r['slug']}",
                        "timestamp": r["last_triggered"],
                        "used_for": "active_rules",
                    })
            except sqlite3.OperationalError:
                pass

            # ── Alert dispatches ──
            try:
                rows = conn.execute(
                    "SELECT rule_slug, severity, ts FROM alert_history WHERE ts >= ? LIMIT 20",
                    (cutoff_24h,),
                ).fetchall()
                for r in rows:
                    trace.append({
                        "source": "alert_dispatcher",
                        "source_id": f"alert:{r['rule_slug']}",
                        "timestamp": r["ts"],
                        "used_for": "suggestions",
                    })
            except sqlite3.OperationalError:
                pass

            conn.close()
        except Exception as exc:
            logger.debug("Evidence trace build failed: %s", exc)

        return trace