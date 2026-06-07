"""
Helios v6 — Operational Insight Engine (Phase 5).

Transforms Helios into an operational intelligence
dashboard without changing the deterministic core.

Five sub-systems:
  5.1 Timeline Explorer     — navigable event/session/narrative views
  5.2 Trend Engine          — rolling-window operational trend computation
  5.3 Correlation Explorer   — inspectable correlation inspection
  5.4 Narrative Diffing      — "what changed recently?"
  5.5 Evidence Visualization — trace every output back to source IDs

Every export has a stable contract (JSON schema). The visualization
layer consumes contracts, not internal DB structure.

NOT: new cognition, autonomy, opaque ML, emotional interpretation.
"""
from __future__ import annotations

import json, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("helios.insight")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
INSIGHT_DIR = DATA_DIR / "insights"
INSIGHT_DIR.mkdir(parents=True, exist_ok=True)

INSIGHT_SCHEMA_VERSION = "1.0"

METRIC_LABELS = {
    "sleep.hours": "Sleep", "activity.minutes_daily": "Activity",
    "mood.score_daily": "Mood", "resting_heart_rate.avg_daily": "Resting HR",
    "spotify.listen_minutes_daily": "Spotify", "spotify.tracks_daily": "Tracks",
    "weather.temp_max": "High Temp", "weather.precipitation": "Precipitation",
    "protein.grams_daily": "Protein", "location.gps_valid": "GPS Valid",
}

# ── Atomic write helper ───────────────────────────────────────────────────

def _write_insight_json(path: Path, data: dict, contract_name: str) -> None:
    """Write an insight JSON export atomically, injecting schema_version."""
    data["schema_version"] = INSIGHT_SCHEMA_VERSION
    data["contract"] = contract_name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)

# ==========================================================================
# STABLE EXPORT CONTRACTS — visualization layer consumes these
# ==========================================================================

def contract_timeline_explorer() -> dict:
    """Stable schema for timeline explorer API."""
    return {
        "api_version": "1.0",
        "endpoint": "timeline_explorer",
        "description": "Navigable deterministic views over events, sessions, narratives",
        "filters": ["date_key", "event_type", "session_type", "min_importance",
                    "min_salience", "window_days"],
        "outputs": {
            "events": "timeline_events filtered + sorted by importance",
            "sessions": "timeline_sessions with salience scores",
            "narratives": "daily narrative statements with evidence chains",
            "anomalies": "metric_anomaly events with parsed metadata",
            "links": "event_links with resolved target summaries",
        },
    }

def contract_trend_api() -> dict:
    """Stable schema for trend engine."""
    return {
        "api_version": "1.0",
        "endpoint": "trend_engine",
        "description": "Rolling-window operational trend computation",
        "windows": ["7d", "30d", "90d"],
        "metrics": ["sleep", "activity", "mood", "focus_continuity",
                    "alert_frequency", "gaming_duration", "resting_hr"],
        "outputs": {
            "trends": [{"metric": "str", "window_days": "int",
                       "direction": "str", "slope": "float",
                       "confidence": "float", "sample_size": "int",
                       "change_pct": "float", "baseline_avg": "float",
                       "current_avg": "float", "threshold_breached": "bool"}],
        },
    }
def contract_correlation_api() -> dict:
    """Stable schema for correlation explorer."""
    return {
        "api_version": "1.0",
        "endpoint": "correlation_explorer",
        "description": "Inspectable correlation exploration with explicit warnings",
        "warning": "correlation ≠ causation",
        "outputs": {
            "correlations": [{"metric_a": "str", "metric_b": "str", "r": "float",
                             "p_value": "float", "strength": "str", "window_days": "int",
                             "sample_pairs": "int", "direction": "str",
                             "evidence_ids": "list[str]", "disclaimer": "correlation only"}],
        },
    }

def contract_narrative_diff() -> dict:
    """Stable schema for narrative diffing."""
    return {
        "api_version": "1.0",
        "endpoint": "narrative_diff",
        "description": "'What changed recently?' — deterministic cross-period comparison",
        "outputs": {
            "changes": [{"metric_or_theme": "str", "period_a": "str", "period_b": "str",
                        "direction": "str", "change_pct": "float", "confidence": "float",
                        "summary": "str", "evidence_ids": "list[str]"}],
        },
    }

def contract_evidence_viz() -> dict:
    """Stable schema for evidence visualization."""
    return {
        "api_version": "1.0",
        "endpoint": "evidence_visualization",
        "description": "Every output traceable back to source event/session/correlation IDs",
        "invariant": "No summary hides its evidence chain",
        "outputs": {
            "trace": [{"source_type": "str", "source_id": "int",
                      "target_type": "str", "target_id": "int",
                      "relationship": "str", "confidence": "float",
                      "evidence_text": "str"}],
        },
    }

ALL_CONTRACTS = {
    "timeline_explorer": contract_timeline_explorer(),
    "trend": contract_trend_api(),
    "correlation": contract_correlation_api(),
    "narrative_diff": contract_narrative_diff(),
    "evidence": contract_evidence_viz(),
}


# ==========================================================================
# 5.1 — TIMELINE EXPLORER
# ==========================================================================

def explore_timeline(db_conn, *, window_days: int = 7,
                     min_importance: float = 0.3,
                     min_salience: float = 0.3,
                     event_type: str = "",
                     date_key: str = "") -> dict:
    """Build navigable timeline view: events + sessions + narratives + links."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    results: dict[str, Any] = {
        "contract": "timeline_explorer_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": window_days, "cutoff_date": cutoff,
    }

    # Events — top by importance
    sql = ("SELECT id, ts, event_type, source_module, importance, summary, "
           "date_key FROM timeline_events WHERE importance >= ? AND date_key >= ?")
    params: list[Any] = [min_importance, cutoff]
    if event_type:
        sql += " AND event_type = ?"; params.append(event_type)
    if date_key:
        sql += " AND date_key = ?"; params.append(date_key)
    sql += " ORDER BY importance DESC, ts DESC LIMIT 100"

    events = []
    for r in db_conn.execute(sql, tuple(params)).fetchall():
        events.append({
            "id": r[0], "ts": r[1], "type": r[2],
            "source": r[3], "importance": r[4], "summary": r[5][:120],
            "date_key": r[6],
        })
    results["events"] = events
    results["event_count"] = len(events)

    # Sessions — with salience
    sql2 = ("SELECT id, session_type, date_key, session_start, session_end, "
            "dominant_state, event_count, summary, importance, novelty, "
            "confidence, duration_secs FROM timeline_sessions "
            "WHERE date_key >= ? ORDER BY importance DESC LIMIT 50")
    sessions = []
    for r in db_conn.execute(sql2, (cutoff,)).fetchall():
        dur_h = r[10] / 3600 if r[10] else 0
        sal = (r[8] or 0) * 0.45 + (r[9] or 0.3) * 0.35 + (r[10] or 0.5) * 0.20
        if sal < min_salience:
            continue
        sessions.append({
            "id": r[0], "type": r[1], "date_key": r[2],
            "start": r[3], "end": r[4], "state": r[5],
            "event_count": r[6], "summary": r[7][:120],
            "importance": r[8], "novelty": r[9],
            "confidence": r[10], "duration_h": round(dur_h, 1),
            "salience": round(sal, 3),
        })
    results["sessions"] = sessions
    results["session_count"] = len(sessions)

    # Anomalies — with metadata
    anomalies = []
    for r in db_conn.execute(
        "SELECT id, ts, summary, importance, metadata, date_key FROM timeline_events "
        "WHERE event_type='metric_anomaly' AND date_key >= ? "
        "ORDER BY importance DESC LIMIT 30", (cutoff,),
    ).fetchall():
        meta = json.loads(r[4]) if r[4] else {}
        anomalies.append({
            "id": r[0], "ts": r[1], "summary": r[2][:120],
            "importance": r[3], "date_key": r[5],
            "metric": meta.get("metric", ""),
            "value": meta.get("value"), "z_score": meta.get("z_score", 0),
            "direction": meta.get("direction", ""),
        })
    results["anomalies"] = anomalies
    results["anomaly_count"] = len(anomalies)

    # Event type breakdown
    type_counts = {}
    for e in events:
        t = e["type"]
        type_counts[t] = type_counts.get(t, 0) + 1
    results["type_breakdown"] = type_counts

    # Session state breakdown
    state_counts = {}
    for s in sessions:
        st = s["state"] or "unknown"
        state_counts[st] = state_counts.get(st, 0) + 1
    results["state_breakdown"] = state_counts

    return results


# ==========================================================================
# 5.2 — TREND ENGINE
# ==========================================================================

def compute_trends(db_conn, *, windows: list[int] = None,
                   metrics: list[str] = None) -> dict:
    """Compute rolling-window operational trends deterministically."""
    if windows is None:
        windows = [7, 30, 90]
    if metrics is None:
        metrics = ["sleep.hours", "activity.minutes_daily", "mood.score_daily",
                   "resting_heart_rate.avg_daily", "focus.continuity"]

    results: dict[str, Any] = {
        "contract": "trend_engine_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trends": [], "warnings": ["Trends are measured, not interpreted. "
                                   "Direction reflects numerical change, not 'good' or 'bad'."],
    }

    for metric in metrics:
        for window in windows:
            trend = _compute_single_trend(db_conn, metric, window)
            if trend:
                results["trends"].append(trend)

    # Sort: strongest trends first
    results["trends"].sort(key=lambda t: abs(t.get("change_pct", 0)), reverse=True)

    return results


def _compute_single_trend(db_conn, metric: str, window_days: int) -> dict | None:
    """Compute trend for one metric over one window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")

    rows = db_conn.execute(
        "SELECT date_key, value FROM metric_snapshots "
        "WHERE metric = ? AND date_key >= ? ORDER BY date_key",
        (metric, cutoff),
    ).fetchall()

    if len(rows) < max(3, window_days // 3):  # need enough data points
        return None

    values = [float(r[1]) for r in rows]
    n = len(values)

    # Linear regression slope
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    slope = numerator / denominator if denominator else 0

    # Change magnitude
    first_half = values[:n // 2]
    second_half = values[n // 2:]
    first_avg = sum(first_half) / len(first_half)
    second_avg = sum(second_half) / len(second_half)
    change_pct = abs((second_avg - first_avg) / max(first_avg, 0.001)) * 100
    direction = "improving" if second_avg > first_avg else "declining"

    # For sleep/mood/HR: lower is usually declining regardless
    if metric in ("sleep.hours", "mood.score_daily"):
        direction = "declining" if second_avg < first_avg else "improving"

    # Confidence based on sample size and slope magnitude
    conf = min(0.95, 0.4 + (n / window_days) * 0.3 + abs(slope) * 10)

    label = METRIC_LABELS.get(metric, metric)

    return {
        "metric": metric, "label": label, "window_days": window_days,
        "direction": direction, "slope": round(slope, 4),
        "confidence": round(conf, 3), "sample_size": n,
        "change_pct": round(change_pct, 1),
        "first_avg": round(first_avg, 2), "second_avg": round(second_avg, 2),
        "baseline_avg": round(y_mean, 2), "current_avg": round(second_avg, 2),
        "threshold_breached": abs(change_pct) > 25,
    }


def compute_focus_trends(db_conn, window_days: int = 7) -> list[dict]:
    """Trend for focus states using focus_daily_summary."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")

    trends = []
    for state in ("working", "gaming"):
        rows = db_conn.execute(
            "SELECT date_key, total_secs FROM focus_daily_summary "
            "WHERE state = ? AND date_key >= ? ORDER BY date_key",
            (state, cutoff),
        ).fetchall()
        if len(rows) < 3:
            continue

        hours = [float(r[1]) / 3600 for r in rows]
        n = len(hours)
        first_avg = sum(hours[:n // 2]) / (n // 2)
        second_avg = sum(hours[n // 2:]) / (n - n // 2)
        change = abs((second_avg - first_avg) / max(first_avg, 0.01)) * 100
        direction = "increasing" if second_avg > first_avg else "declining"

        trends.append({
            "metric": f"focus.{state}.hours", "label": f"{state.capitalize()} focus",
            "window_days": window_days, "direction": direction,
            "confidence": min(0.85, 0.5 + n / 20),
            "sample_size": n, "change_pct": round(change, 1),
            "first_avg": round(first_avg, 1), "second_avg": round(second_avg, 1),
            "threshold_breached": change > 30,
        })

    return trends


# ==========================================================================
# 5.3 — CORRELATION EXPLORER
# ==========================================================================

def explore_correlations(db_conn) -> dict:
    """Inspectable correlation exploration with explicit warnings."""
    rows = db_conn.execute(
        "SELECT id, ts, metric_a, metric_b, window_days, pearson_r, "
        "p_value, strength FROM correlations ORDER BY ABS(pearson_r) DESC"
    ).fetchall()

    correlations = []
    for r in rows:
        la = METRIC_LABELS.get(r[2], r[2])
        lb = METRIC_LABELS.get(r[3], r[3])
        direction = "positive" if r[5] > 0 else "negative"

        # Count paired observations from correlation_observations
        obs = db_conn.execute(
            "SELECT COUNT(*) FROM correlation_observations "
            "WHERE metric_a = ? AND metric_b = ?",
            (r[2], r[3]),
        ).fetchone()
        sample_pairs = obs[0] if obs else 0

        correlations.append({
            "id": r[0], "metric_a": r[2], "metric_b": r[3],
            "label_a": la, "label_b": lb,
            "r": round(r[5], 3), "p_value": round(r[6], 4),
            "strength": r[7], "window_days": r[4],
            "sample_pairs": sample_pairs, "direction": direction,
            "evidence_ids": [f"corr_{r[0]}"],
            "disclaimer": "Correlation only — does not imply causation.",
            "strength_color": {"strong": "🟢", "moderate": "🟡",
                              "weak": "⚪", "spurious": "❌"}.get(r[7], "⚪"),
        })

    # Flag potential false positives: p > 0.05 or < 5 sample pairs
    flags = []
    for c in correlations:
        if c["p_value"] > 0.05:
            flags.append({
                "warning": f"{c['label_a']}↔{c['label_b']} may be spurious (p={c['p_value']})",
                "correlation_id": c["id"],
            })
        if c["sample_pairs"] < 5:
            flags.append({
                "warning": f"{c['label_a']}↔{c['label_b']} has only {c['sample_pairs']} samples — low confidence",
                "correlation_id": c["id"],
            })

    return {
        "contract": "correlation_explorer_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "correlations": correlations,
        "count": len(correlations),
        "false_positive_flags": flags,
        "disclaimer_global": "ALL correlations shown are statistical associations. "
                            "None imply causation. Strength indicates mathematical "
                            "relationship, not operational truth.",
    }


# ==========================================================================
# 5.4 — NARRATIVE DIFFING
# ==========================================================================

def diff_narratives(db_conn, *, period_a_days: int = 14,
                    period_b_days: int = 7) -> dict:
    """'What changed recently?' — deterministic cross-period comparison."""
    now = datetime.now(timezone.utc)

    # Period A: farther back (e.g., 14 days ago to 7 days ago)
    a_end = (now - timedelta(days=period_b_days)).strftime("%Y-%m-%d")
    a_start = (now - timedelta(days=period_a_days)).strftime("%Y-%m-%d")

    # Period B: recent (e.g., last 7 days)
    b_start = (now - timedelta(days=period_b_days)).strftime("%Y-%m-%d")
    b_end = now.strftime("%Y-%m-%d")

    changes: list[dict] = []

    # 1. Metric-level diffs
    for metric in ("sleep.hours", "activity.minutes_daily", "mood.score_daily",
                   "resting_heart_rate.avg_daily"):
        a_avg = _period_avg(db_conn, metric, a_start, a_end)
        b_avg = _period_avg(db_conn, metric, b_start, b_end)

        if a_avg is None or b_avg is None:
            continue

        change = abs((b_avg - a_avg) / max(a_avg, 0.001))
        if change < 0.1 and abs(b_avg - a_avg) < 1:
            continue  # trivial

        direction = "increased" if b_avg > a_avg else "decreased"
        label = METRIC_LABELS.get(metric, metric)
        summary = f"{label} {direction} from {a_avg:.1f} to {b_avg:.1f} "

        if "sleep" in metric:
            summary += f"(healthy range: 7–9h)"
        elif "activity" in metric:
            summary += f"(target: 30+ min)"
        elif "mood" in metric:
            summary += f"(scale: 1–9)"

        changes.append({
            "metric_or_theme": metric, "period_a": f"{a_start} to {a_end}",
            "period_b": f"{b_start} to {b_end}",
            "direction": direction, "change_pct": round(change * 100, 1),
            "confidence": min(0.9, 0.5 + change),
            "summary": summary, "evidence_ids": [f"metric:{metric}"],
        })

    # 2. Alert frequency diff
    a_alerts = db_conn.execute(
        "SELECT COUNT(*) FROM alert_history WHERE ts >= ? AND ts < ?",
        (a_start, a_end),
    ).fetchone()[0]
    b_alerts = db_conn.execute(
        "SELECT COUNT(*) FROM alert_history WHERE ts >= ? AND ts < ?",
        (b_start, b_end),
    ).fetchone()[0]

    if a_alerts > 0:
        alert_change = abs((b_alerts - a_alerts) / a_alerts)
        if alert_change > 0.2 or abs(b_alerts - a_alerts) >= 5:
            direction = "increased" if b_alerts > a_alerts else "decreased"
            changes.append({
                "metric_or_theme": "alert_frequency",
                "period_a": f"{a_start} to {a_end}",
                "period_b": f"{b_start} to {b_end}",
                "direction": direction,
                "change_pct": round(alert_change * 100, 1),
                "confidence": 0.8, "sample_size": b_alerts,
                "summary": f"Alert frequency {direction}: {a_alerts} → {b_alerts} alerts "
                          f"({round(alert_change * 100, 1)}% change)",
                "evidence_ids": ["source:alert_history"],
            })

    # 3. Focus-state diffs
    for state in ("working", "gaming"):
        a_focus = db_conn.execute(
            "SELECT AVG(total_secs) FROM focus_daily_summary "
            "WHERE state = ? AND date_key >= ? AND date_key < ?",
            (state, a_start, a_end),
        ).fetchone()[0]
        b_focus = db_conn.execute(
            "SELECT AVG(total_secs) FROM focus_daily_summary "
            "WHERE state = ? AND date_key >= ? AND date_key < ?",
            (state, b_start, b_end),
        ).fetchone()[0]

        if a_focus and b_focus:
            a_h = float(a_focus) / 3600; b_h = float(b_focus) / 3600
            change = abs((b_h - a_h) / max(a_h, 0.01))
            if change > 0.15:
                direction = "increased" if b_h > a_h else "decreased"
                changes.append({
                    "metric_or_theme": f"focus.{state}",
                    "period_a": f"{a_start} to {a_end}",
                    "period_b": f"{b_start} to {b_end}",
                    "direction": direction, "change_pct": round(change * 100, 1),
                    "confidence": min(0.85, 0.5 + change),
                    "summary": f"{state.capitalize()} focus {direction}: "
                              f"{a_h:.1f}h → {b_h:.1f}h per day",
                    "evidence_ids": ["source:focus_daily_summary"],
                })

    # Sort by significance
    changes.sort(key=lambda c: c.get("change_pct", 0), reverse=True)

    return {
        "contract": "narrative_diff_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_a_label": f"{period_a_days - period_b_days} days", "period_a": f"{a_start} to {a_end}",
        "period_b_label": f"last {period_b_days} days", "period_b": f"{b_start} to {b_end}",
        "changes": changes[:12],  # top 12 changes
        "change_count": len(changes),
        "disclaimer": "Changes are measured numerically. Direction reflects raw delta, not interpretation.",
    }


def _period_avg(db_conn, metric: str, start: str, end: str) -> float | None:
    r = db_conn.execute(
        "SELECT AVG(value) FROM metric_snapshots "
        "WHERE metric = ? AND date_key >= ? AND date_key < ?",
        (metric, start, end),
    ).fetchone()
    if r and r[0] is not None:
        return float(r[0])
    return None


# ==========================================================================
# 5.5 — EVIDENCE VISUALIZATION
# ==========================================================================

def trace_evidence(db_conn, *, source_type: str = "",
                   source_id: int = 0, limit: int = 30) -> dict:
    """Trace any output back to its source event/session/correlation IDs.

    This is Helios' strongest differentiator. Protect it aggressively.
    """
    traces: list[dict] = []

    if source_type == "narrative" or not source_type:
        # Show how narratives link to events
        rows = db_conn.execute(
            "SELECT id, statement, evidence, confidence, template_id "
            "FROM narrative_export ORDER BY id DESC LIMIT ?", (limit // 3,)
        ).fetchall() if False else []  # narrative_export table doesn't exist yet
        # Instead, trace from notable events
        for r in db_conn.execute(
            "SELECT ne.id, ne.event_type, ne.summary, ne.timeline_event_id, "
            "ne.session_id, ne.importance, te.event_type as te_type "
            "FROM notable_events ne "
            "LEFT JOIN timeline_events te ON te.id = ne.timeline_event_id "
            "ORDER BY ne.date_key DESC, ne.rank LIMIT ?",
            (limit,),
        ).fetchall():
            evt_id = r[3]
            sess_id = r[4]

            if evt_id:
                evt = db_conn.execute(
                    "SELECT summary, importance, event_type FROM timeline_events "
                    "WHERE id = ?", (evt_id,),
                ).fetchone()
                if evt:
                    traces.append({
                        "source_type": "notable_event",
                        "source_id": r[0], "relationship": "derived_from",
                        "target_type": "timeline_event", "target_id": evt_id,
                        "confidence": r[5] or 0.7,
                        "source_summary": r[2][:100],
                        "target_summary": evt[0][:100],
                        "target_importance": evt[1],
                        "target_event_type": evt[2],
                    })

            if sess_id:
                sess = db_conn.execute(
                    "SELECT summary, session_type, importance FROM timeline_sessions "
                    "WHERE id = ?", (sess_id,),
                ).fetchone()
                if sess:
                    traces.append({
                        "source_type": "notable_event",
                        "source_id": r[0], "relationship": "summarizes",
                        "target_type": "timeline_session", "target_id": sess_id,
                        "confidence": r[5] or 0.7,
                        "source_summary": r[2][:100],
                        "target_summary": sess[0][:100],
                        "target_session_type": sess[1],
                    })

    if source_type == "session" or not source_type:
        # Session → events mapping
        sessions = db_conn.execute(
            "SELECT id, session_type, summary, source_events FROM timeline_sessions "
            "WHERE (? = 0 OR id = ?) ORDER BY id DESC LIMIT ?",
            (source_id, source_id, limit),
        ).fetchall()

        for sess in sessions:
            evt_ids = json.loads(sess[3]) if sess[3] else []
            for eid in evt_ids[:5]:  # first 5 per session
                evt = db_conn.execute(
                    "SELECT summary, event_type, importance FROM timeline_events "
                    "WHERE id = ?", (eid,),
                ).fetchone()
                if evt:
                    traces.append({
                        "source_type": "timeline_session",
                        "source_id": sess[0], "relationship": "contains",
                        "target_type": "timeline_event", "target_id": eid,
                        "confidence": 0.9,
                        "source_summary": sess[2][:100],
                        "target_summary": evt[0][:100],
                        "target_event_type": evt[1],
                    })

    if source_type == "correlation" or not source_type:
        # Correlation → observations
        corrs = db_conn.execute(
            "SELECT id, metric_a, metric_b, pearson_r, strength FROM correlations "
            "WHERE (? = 0 OR id = ?) LIMIT ?",
            (source_id, source_id, limit // 2),
        ).fetchall()

        for corr in corrs:
            obs = db_conn.execute(
                "SELECT COUNT(*) FROM correlation_observations "
                "WHERE metric_a = ? AND metric_b = ?",
                (corr[1], corr[2]),
            ).fetchone()

            la = METRIC_LABELS.get(corr[1], corr[1])
            lb = METRIC_LABELS.get(corr[2], corr[2])

            traces.append({
                "source_type": "correlation",
                "source_id": corr[0], "relationship": "statistical_association",
                "target_type": "correlation_observations",
                "target_id": f"obs_{corr[1]}_{corr[2]}",
                "confidence": 0.8 if corr[4] == "strong" else 0.6,
                "source_summary": f"{la} ↔ {lb} (r={corr[3]:.3f}, {corr[4]})",
                "target_summary": f"{obs[0]} paired observations",
                "warning": "Correlation ≠ causation",
            })

    return {
        "contract": "evidence_visualization_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "traces": traces[:limit],
        "trace_count": len(traces),
        "invariant": "Every trace maps to concrete event/session/correlation IDs. "
                    "No opaque summaries. No hidden evidence chains.",
    }


# ==========================================================================
# MASTER ORCHESTRATOR — runs all five, saves exports
# ==========================================================================

def generate_all_insights(db_conn, *, window_days: int = 7) -> dict:
    """Run all five Phase 5 sub-systems and save export files."""
    result = {"generated_at": datetime.now(timezone.utc).isoformat()}

    # 5.1 Timeline Explorer
    tl = explore_timeline(db_conn, window_days=window_days)
    _write_insight_json(INSIGHT_DIR / "timeline_explorer.json", tl, "timeline_explorer_v1")
    result["timeline_explorer"] = {"event_count": tl["event_count"],
                                   "session_count": tl["session_count"]}

    # 5.2 Trend Engine
    trends = compute_trends(db_conn)
    focus_trends = compute_focus_trends(db_conn, window_days)
    trends["focus_trends"] = focus_trends
    _write_insight_json(INSIGHT_DIR / "trends.json", trends, "trend_v1")
    result["trends"] = {"trend_count": len(trends.get("trends", []))}

    # 5.3 Correlation Explorer
    corr = explore_correlations(db_conn)
    _write_insight_json(INSIGHT_DIR / "correlations.json", corr, "correlation_v1")
    result["correlations"] = {"correlation_count": corr["count"]}

    # 5.4 Narrative Diffing
    diff = diff_narratives(db_conn)
    _write_insight_json(INSIGHT_DIR / "narrative_diff.json", diff, "narrative_diff_v1")
    result["narrative_diff"] = {"change_count": diff["change_count"]}

    # 5.5 Evidence Visualization
    ev = trace_evidence(db_conn)
    _write_insight_json(INSIGHT_DIR / "evidence_traces.json", ev, "evidence_v1")
    result["evidence"] = {"trace_count": ev["trace_count"]}

    # Contract registry — safe to serialize now that types are strings
    _write_insight_json(INSIGHT_DIR / "_contracts.json", dict(ALL_CONTRACTS), "contract_registry_v1")

    log.info("Insight engine: %d traces, %d diffs, %d trends generated",
             ev["trace_count"], diff["change_count"],
             len(trends.get("trends", [])))

    return result
