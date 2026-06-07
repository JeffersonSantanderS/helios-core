"""Helios v6 - Query Engine.

Answers grounded operational questions from the
timeline infrastructure. Every answer is deterministic, evidence-backed,
and traceable to event/session/correlation IDs.

Architecture:
  question → query planner → evidence retrieval → answer synthesis

NOT: chatbot improvisation, emotional interpretation, invented causality.
"""
from __future__ import annotations

import json, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("helios.query_engine")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
EXPORT_DIR = DATA_DIR / "query_exports"

# ==========================================================================
# QUERY PRIMITIVES — deterministic retrieval functions
# ==========================================================================

def fetch_events(db_conn, *, date_key: str = "", event_type: str = "",
                 min_importance: float = 0.0, limit: int = 50,
                 days_back: int = 0) -> list[dict]:
    """Fetch timeline events filtered by criteria."""
    sql = "SELECT id, ts, event_type, source_module, importance, summary, metadata, date_key FROM timeline_events WHERE 1=1"
    params: list[Any] = []

    if date_key:
        sql += " AND date_key = ?"; params.append(date_key)
    if event_type:
        sql += " AND event_type = ?"; params.append(event_type)
    if min_importance > 0:
        sql += " AND importance >= ?"; params.append(min_importance)
    if days_back > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        sql += " AND date_key >= ?"; params.append(cutoff)

    sql += " ORDER BY importance DESC, ts DESC LIMIT ?"; params.append(limit)

    rows = db_conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_dict(r) for r in rows]


def fetch_sessions(db_conn, *, date_key: str = "", session_type: str = "",
                   min_importance: float = 0.0, limit: int = 20,
                   days_back: int = 0) -> list[dict]:
    """Fetch timeline sessions with salience scores."""
    sql = ("SELECT id, session_type, date_key, session_start, session_end, "
           "duration_secs, dominant_state, event_count, summary, "
           "importance, novelty, confidence FROM timeline_sessions WHERE 1=1")
    params: list[Any] = []

    if date_key:
        sql += " AND date_key = ?"; params.append(date_key)
    if session_type:
        sql += " AND session_type = ?"; params.append(session_type)
    if min_importance > 0:
        sql += " AND importance >= ?"; params.append(min_importance)
    if days_back > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        sql += " AND date_key >= ?"; params.append(cutoff)

    sql += " ORDER BY importance DESC LIMIT ?"; params.append(limit)

    rows = db_conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_dict(r) for r in rows]


def fetch_correlations(db_conn, *, strength: str = "", limit: int = 10) -> list[dict]:
    """Fetch correlations, optionally filtered by strength."""
    sql = ("SELECT id, ts, metric_a, metric_b, window_days, pearson_r, "
           "p_value, strength FROM correlations WHERE 1=1")
    params: list[Any] = []

    if strength:
        sql += " AND strength = ?"; params.append(strength)
    sql += " ORDER BY ABS(pearson_r) DESC LIMIT ?"; params.append(limit)

    rows = db_conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_dict(r) for r in rows]


def fetch_anomalies(db_conn, *, date_key: str = "", metric: str = "",
                    min_z: float = 2.0, limit: int = 20,
                    days_back: int = 0) -> list[dict]:
    """Fetch metric anomalies with parsed metadata."""
    sql = ("SELECT id, ts, summary, importance, metadata, date_key "
           "FROM timeline_events WHERE event_type = 'metric_anomaly'")
    params: list[Any] = []

    if date_key:
        sql += " AND date_key = ?"; params.append(date_key)
    if days_back > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        sql += " AND date_key >= ?"; params.append(cutoff)
    sql += " ORDER BY importance DESC LIMIT ?"; params.append(limit)

    rows = db_conn.execute(sql, tuple(params)).fetchall()
    results = []
    for r in rows:
        d = _row_to_dict(r)
        meta = json.loads(d.pop("metadata", "{}") or "{}")
        d["metric"] = meta.get("metric", "")
        d["value"] = meta.get("value")
        d["baseline_avg"] = meta.get("baseline_avg")
        d["z_score"] = meta.get("z_score", 0)
        d["direction"] = meta.get("direction", "")

        # Apply filters on parsed metadata
        if metric and d["metric"] != metric:
            continue
        if d["z_score"] < min_z:
            continue
        results.append(d)

    return results[:limit]


def fetch_notable_events(db_conn, *, date_key: str = "", limit: int = 10) -> list[dict]:
    """Fetch notable events for a given date."""
    sql = ("SELECT id, date_key, rank, event_type, session_id, timeline_event_id, "
           "summary, importance, novelty, confidence FROM notable_events WHERE 1=1")
    params: list[Any] = []

    if date_key:
        sql += " AND date_key = ?"; params.append(date_key)
    sql += " ORDER BY date_key DESC, rank ASC LIMIT ?"; params.append(limit)

    rows = db_conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_dict(r) for r in rows]


def fetch_event_links(db_conn, *, event_id: int = 0, link_type: str = "",
                      limit: int = 50) -> list[dict]:
    """Fetch links for a given event."""
    sql = ("SELECT el.id, el.source_event_id, el.target_event_id, el.link_type, "
           "el.confidence, el.evidence, "
           "te.event_type as target_type, te.summary as target_summary "
           "FROM event_links el "
           "JOIN timeline_events te ON te.id = el.target_event_id WHERE 1=1")
    params: list[Any] = []

    if event_id:
        sql += " AND el.source_event_id = ?"; params.append(event_id)
    if link_type:
        sql += " AND el.link_type = ?"; params.append(link_type)
    sql += " ORDER BY el.confidence DESC LIMIT ?"; params.append(limit)

    rows = db_conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_dict(r) for r in rows]


# ==========================================================================
# QUERY PLANNER — maps question types to retrieval pipelines
# ==========================================================================

# Question type taxonomy
QT_IMPORTANCE   = "importance_query"    # "most important events this week?"
QT_TREND        = "trend_query"         # "when did sleep degrade?"
QT_CORRELATION  = "correlation_query"   # "what correlates with low focus?"
QT_TIMELINE     = "timeline_query"      # "what happened on Tuesday?"
QT_ANOMALY      = "anomaly_query"       # "were there any anomalies?"
QT_CAUSAL_CHAIN = "causal_chain"        # "what preceded the productivity drop?"
QT_STATUS       = "status_query"        # "how's my health this week?"

# Patterns that map natural language to query types + parameters
QUERY_PATTERNS = [
    # Importance patterns
    (QT_IMPORTANCE, ["most important", "top events", "significant",
                     "notable", "key events", "highlights"]),
    # Trend patterns
    (QT_TREND, ["trend", "degrad", "decline", "improving", "getting worse",
                "getting better", "pattern", "over time"]),
    # Correlation patterns
    (QT_CORRELATION, ["correlat", "link", "relat", "connected",
                      "affect", "impact", "influenc"]),
    # Timeline patterns
    (QT_TIMELINE, ["happened", "occurred", "timeline", "day",
                   "tuesday", "wednesday", "thursday", "friday",
                   "saturday", "sunday", "monday", "yesterday"]),
    # Anomaly patterns
    (QT_ANOMALY, ["anomal", "unusual", "outlier", "spike",
                  "drop", "abnormal", "strange"]),
    # Causal chain patterns
    (QT_CAUSAL_CHAIN, ["preced", "before", "led to", "caused",
                       "trigger", "happened before"]),
    # Status patterns
    (QT_STATUS, ["health", "status", "how am", "how are",
                 "summary", "overview", "report", "check"]),
]


def plan_query(question: str) -> tuple[str, dict]:
    """Classify a natural-language question and extract parameters.

    Returns (query_type, params) where params may contain:
      - metric: specific metric if mentioned (sleep, activity, mood, etc.)
      - days_back: temporal scope
      - subject: what the question is about
    """
    q_lower = question.lower()

    # Determine query type by keyword matching
    scores: dict[str, int] = {}
    for qtype, keywords in QUERY_PATTERNS:
        score = sum(1 for kw in keywords if kw in q_lower)
        if score > 0:
            scores[qtype] = score

    if not scores:
        query_type = QT_STATUS  # default: general status query
    else:
        query_type = max(scores, key=scores.get)

    # Extract parameters
    params: dict[str, Any] = {}

    # Metric detection
    metric_map = {
        "sleep": ("sleep.hours", ["sleep", "rest", "slept"]),
        "activity": ("activity.minutes_daily", ["activity", "exercise", "movement", "active"]),
        "mood": ("mood.score_daily", ["mood", "feeling", "emotion"]),
        "hr": ("resting_heart_rate.avg_daily", ["heart rate", "hr", "pulse", "bpm"]),
        "focus": ("focus", ["focus", "concentration", "productivity", "work"]),
        "gaming": ("gaming", ["gaming", "game", "play"]),
    }
    for key, (metric_id, terms) in metric_map.items():
        if any(t in q_lower for t in terms):
            params["metric"] = metric_id
            params["metric_label"] = key
            break

    # Time scope
    if "week" in q_lower or "7 day" in q_lower:
        params["days_back"] = 7
    elif "month" in q_lower or "30 day" in q_lower:
        params["days_back"] = 30
    elif "today" in q_lower:
        params["days_back"] = 1
    else:
        params["days_back"] = 3  # default: 3 days

    # Subject extraction (what the question is about after the query word)
    params["question"] = question
    params["query_type"] = query_type

    return query_type, params


# ==========================================================================
# ANSWER SYNTHESIS — deterministic template-based responses
# ==========================================================================

METRIC_LABELS = {
    "sleep.hours": "Sleep", "activity.minutes_daily": "Activity",
    "mood.score_daily": "Mood", "resting_heart_rate.avg_daily": "Resting HR",
    "spotify.listen_minutes_daily": "Spotify", "spotify.tracks_daily": "Tracks",
}


def answer_importance(db_conn, params: dict) -> dict:
    """Answer: 'What were the most important events?'"""
    days = params.get("days_back", 3)
    events = fetch_events(db_conn, min_importance=0.6, days_back=days, limit=10)
    sessions = fetch_sessions(db_conn, min_importance=0.5, days_back=days, limit=5)

    statements = []
    evidence = []

    for e in events[:5]:
        statements.append(f"• {e['summary']} ({e['confidence']*100 if e.get('confidence') else 75:.0f}% conf)")
        evidence.append(f"evt_{e['id']}")

    for s in sessions[:3]:
        statements.append(f"• [{s['session_type']}] {s['summary']} "
                         f"(imp={s['importance']:.2f}, nov={s['novelty']:.2f})")
        evidence.append(f"sess_{s['id']}")

    return {
        "question": params.get("question", ""),
        "query_type": "importance_query",
        "answer": "\n".join(statements) if statements else "No significant events in this period.",
        "confidence": 0.85 if statements else 0.4,
        "evidence": evidence,
        "source_count": len(events) + len(sessions),
    }


def answer_trend(db_conn, params: dict) -> dict:
    """Answer: 'When did X begin to degrade/improve?'"""
    metric = params.get("metric", "")
    days = params.get("days_back", 7)
    label = METRIC_LABELS.get(metric, metric)

    # Get daily values for the metric
    rows = db_conn.execute(
        "SELECT date_key, value FROM metric_snapshots "
        "WHERE metric = ? AND date_key >= ? ORDER BY date_key",
        (metric, (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")),
    ).fetchall()

    if len(rows) < 3:
        return {
            "question": params.get("question", ""),
            "query_type": "trend_query",
            "answer": f"Not enough {label} data ({len(rows)} days) to assess a trend.",
            "confidence": 0.3, "evidence": [], "source_count": len(rows),
        }

    values = [float(r[1]) for r in rows]
    dates = [r[0] for r in rows]

    # Detect trend: compare first half vs second half
    mid = len(values) // 2
    first_avg = sum(values[:mid]) / mid
    second_avg = sum(values[mid:]) / (len(values) - mid)

    direction = "degrading" if second_avg < first_avg else "improving"
    change_pct = abs((second_avg - first_avg) / max(first_avg, 0.001)) * 100

    # Find inflection point: first date where value crosses the midpoint
    midpoint = (first_avg + second_avg) / 2
    inflection = dates[0]
    for i, v in enumerate(values):
        if direction == "degrading" and v < midpoint:
            inflection = dates[i]; break
        elif direction == "improving" and v > midpoint:
            inflection = dates[i]; break

    if change_pct < 10:
        stmt = f"{label} has been stable (±{change_pct:.0f}%) over the last {days} days."
    elif change_pct < 25:
        stmt = f"{label} is {direction} (shift of {change_pct:.0f}% from early to late period). "
        stmt += f"The trend began around {inflection}."
    else:
        stmt = f"{label} is significantly {direction} — {change_pct:.0f}% change over {days} days. "
        stmt += f"Inflection point: {inflection}."

    return {
        "question": params.get("question", ""),
        "query_type": "trend_query",
        "answer": stmt,
        "confidence": min(0.9, 0.5 + change_pct / 100),
        "evidence": [f"metric:{metric}"],
        "source_count": len(rows),
        "metric": metric, "direction": direction, "change_pct": change_pct,
    }


def answer_correlation(db_conn, params: dict) -> dict:
    """Answer: 'What correlates with X?'"""
    metric = params.get("metric", "")
    label = METRIC_LABELS.get(metric, metric) if metric else "any"

    if not metric:
        # No specific metric — return top correlations overall
        corrs = fetch_correlations(db_conn, limit=5)
        if not corrs:
            return {"question": params.get("question", ""), "query_type": "correlation_query",
                    "answer": "No correlations found yet.", "confidence": 0.3,
                    "evidence": [], "source_count": 0}

        lines = []
        for c in corrs:
            la = METRIC_LABELS.get(c["metric_a"], c["metric_a"])
            lb = METRIC_LABELS.get(c["metric_b"], c["metric_b"])
            lines.append(f"• {la} ↔ {lb}: r={c['pearson_r']:.3f}, "
                        f"p={c['p_value']:.3f} ({c['strength']})")
        return {
            "question": params.get("question", ""),
            "query_type": "correlation_query",
            "answer": "\n".join(lines),
            "confidence": 0.8, "evidence": [f"corr_{c['id']}" for c in corrs],
            "source_count": len(corrs),
        }

    # Find correlations involving this metric
    all_corrs = fetch_correlations(db_conn, limit=20)
    relevant = [c for c in all_corrs
                if c["metric_a"] == metric or c["metric_b"] == metric]

    if not relevant:
        return {
            "question": params.get("question", ""),
            "query_type": "correlation_query",
            "answer": f"No significant correlations found for {label}.",
            "confidence": 0.4, "evidence": [], "source_count": 0,
        }

    lines = []
    for c in sorted(relevant, key=lambda x: abs(x["pearson_r"]), reverse=True)[:5]:
        other = c["metric_b"] if c["metric_a"] == metric else c["metric_a"]
        ol = METRIC_LABELS.get(other, other)
        lines.append(f"• {label} ↔ {ol}: r={c['pearson_r']:.3f}, "
                    f"p={c['p_value']:.3f} ({c['strength']})")

    top = relevant[0]
    other_metric = top["metric_b"] if top["metric_a"] == metric else top["metric_a"]
    other_label = METRIC_LABELS.get(other_metric, other_metric)
    strength = top["strength"]

    stmt = f"{label} correlates most strongly with {other_label} "
    stmt += f"(r={top['pearson_r']:.3f}, {strength}). "

    return {
        "question": params.get("question", ""),
        "query_type": "correlation_query",
        "answer": stmt + "\n" + "\n".join(lines),
        "confidence": 0.75 if strength == "strong" else 0.6,
        "evidence": [f"corr_{c['id']}" for c in relevant],
        "source_count": len(relevant),
    }


def answer_timeline(db_conn, params: dict) -> dict:
    """Answer: 'What happened on X day?'"""
    days = params.get("days_back", 1)
    dk = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    events = fetch_events(db_conn, date_key=dk, min_importance=0.4, limit=15)
    sessions = fetch_sessions(db_conn, date_key=dk, min_importance=0.3, limit=10)
    notable = fetch_notable_events(db_conn, date_key=dk, limit=5)

    evidence = []
    lines = [f"**{dk}**"]

    if notable:
        lines.append("")
        lines.append("**Notable Events:**")
        for n in notable[:3]:
            lines.append(f"• {n['summary']} ({n['confidence']*100:.0f}% conf)")
            evidence.append(f"notable_{n['id']}")

    if sessions:
        lines.append("")
        lines.append("**Sessions:**")
        for s in sessions[:5]:
            dur_h = s.get("duration_secs", 0) / 3600
            lines.append(f"• {s['summary']} ({dur_h:.1f}h)")
            evidence.append(f"sess_{s['id']}")

    if events and not sessions and not notable:
        lines.append("")
        for e in events[:8]:
            lines.append(f"• {e['summary']}")
            evidence.append(f"evt_{e['id']}")

    return {
        "question": params.get("question", ""),
        "query_type": "timeline_query",
        "answer": "\n".join(lines) if len(lines) > 1 else f"No significant events on {dk}.",
        "confidence": 0.8 if (sessions or notable) else 0.5,
        "evidence": evidence, "source_count": len(events) + len(sessions),
    }


def answer_anomaly(db_conn, params: dict) -> dict:
    """Answer: 'Any anomalies?'"""
    days = params.get("days_back", 3)

    anomalies = fetch_anomalies(db_conn, days_back=days, min_z=2.0, limit=10)

    if not anomalies:
        return {
            "question": params.get("question", ""),
            "query_type": "anomaly_query",
            "answer": f"No anomalies detected in the last {days} days.",
            "confidence": 0.7, "evidence": [], "source_count": 0,
        }

    lines = []
    evidence = []
    for a in anomalies[:8]:
        label = METRIC_LABELS.get(a.get("metric", ""), a.get("metric", "?"))
        lines.append(f"• {label}: {a['summary'][:100]} "
                    f"(z={a['z_score']:.1f})")
        evidence.append(f"evt_{a['id']}")

    return {
        "question": params.get("question", ""),
        "query_type": "anomaly_query",
        "answer": f"**{len(anomalies)} anomalies in {days} days:**\n" + "\n".join(lines),
        "confidence": 0.85, "evidence": evidence,
        "source_count": len(anomalies),
    }


def answer_causal_chain(db_conn, params: dict) -> dict:
    """Answer: 'What preceded the drop in X?'"""
    metric = params.get("metric", "")
    label = METRIC_LABELS.get(metric, metric) if metric else "performance"

    # Find the most anomalous event for this metric
    anomalies = fetch_anomalies(db_conn, metric=metric, min_z=2.0, limit=5)

    if not anomalies:
        return {
            "question": params.get("question", ""),
            "query_type": "causal_chain",
            "answer": f"No significant anomalies found for {label}. Unable to trace causal chain.",
            "confidence": 0.3, "evidence": [], "source_count": 0,
        }

    # Get links from the top anomaly event
    top = anomalies[0]
    evt_id = top["id"]

    links = fetch_event_links(db_conn, event_id=evt_id, limit=20)

    if not links:
        return {
            "question": params.get("question", ""),
            "query_type": "causal_chain",
            "answer": (f"The {label} anomaly ({top['summary'][:80]}) has no "
                      f"linked preceding events in the timeline."),
            "confidence": 0.5, "evidence": [f"evt_{evt_id}"], "source_count": 1,
        }

    # Temporal links sorted by confidence
    temporal = [l for l in links if l["link_type"] in ("precedes", "temporal")]
    temporal.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    if not temporal:
        return {
            "question": params.get("question", ""),
            "query_type": "causal_chain",
            "answer": (f"The {label} anomaly has {len(links)} linked events "
                      f"but no clear preceding pattern."),
            "confidence": 0.55, "evidence": [f"evt_{evt_id}"],
            "source_count": len(links),
        }

    lines = [f"{label} anomaly preceded by:"]
    evidence = [f"evt_{evt_id}"]
    for l in temporal[:5]:
        lines.append(f"• {l['target_summary'][:80]} "
                    f"(conf={l['confidence']:.2f}, {l['link_type']})")
        evidence.append(f"link_{l['id']}")

    return {
        "question": params.get("question", ""),
        "query_type": "causal_chain",
        "answer": "\n".join(lines),
        "confidence": min(0.85, temporal[0].get("confidence", 0.5) if temporal else 0.5),
        "evidence": evidence, "source_count": len(temporal),
    }


def answer_status(db_conn, params: dict) -> dict:
    """Answer: 'How's my health/status?' — general overview."""
    days = params.get("days_back", 3)
    mdt_now = datetime.now(timezone.utc) - timedelta(hours=6)
    today = mdt_now.strftime("%Y-%m-%d")

    anomalies = fetch_anomalies(db_conn, days_back=days, min_z=2.0, limit=5)
    notable = fetch_notable_events(db_conn, date_key=today, limit=3)
    sessions = fetch_sessions(db_conn, date_key=today, limit=5)
    correlations = fetch_correlations(db_conn, strength="strong", limit=3)

    evidence = []
    lines = [f"**📊 Helios Status — {mdt_now.strftime('%A, %B %d')}**", ""]

    # Health snapshot: latest daily metrics
    metrics_24h = db_conn.execute(
        "SELECT metric, value, date_key FROM metric_snapshots "
        "WHERE date_key >= ? ORDER BY date_key DESC, metric",
        ((datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d"),),
    ).fetchall()

    seen = set()
    if metrics_24h:
        lines.append("**Latest Metrics:**")
        for m in metrics_24h:
            key = m[0]
            if key in seen:
                continue
            seen.add(key)
            label = METRIC_LABELS.get(key, key)
            try:
                v = float(m[1])
                lines.append(f"• {label}: {v:.1f}")
            except (ValueError, TypeError):
                lines.append(f"• {label}: {m[1]}")
            evidence.append(f"metric:{key}")

    # Anomalies if any
    if anomalies:
        lines.append("")
        lines.append(f"**⚠️ Anomalies ({len(anomalies)}):**")
        for a in anomalies[:3]:
            lines.append(f"• {a['summary'][:100]}")
            evidence.append(f"evt_{a['id']}")

    # Notable events today
    if notable:
        lines.append("")
        lines.append("**Today's Highlights:**")
        for n in notable[:3]:
            lines.append(f"• {n['summary'][:100]}")
            evidence.append(f"notable_{n['id']}")

    # Active correlations
    if correlations:
        lines.append("")
        lines.append("**Active Patterns:**")
        for c in correlations[:2]:
            la = METRIC_LABELS.get(c["metric_a"], c["metric_a"])
            lb = METRIC_LABELS.get(c["metric_b"], c["metric_b"])
            lines.append(f"• {la} ↔ {lb} ({c['strength']}, r={c['pearson_r']:.2f})")
            evidence.append(f"corr_{c['id']}")

    # Work/gaming session summary
    if sessions:
        work = sum(s.get("duration_secs", 0) for s in sessions
                   if s.get("dominant_state") == "working") / 3600
        game = sum(s.get("duration_secs", 0) for s in sessions
                   if s.get("dominant_state") == "gaming") / 3600
        if work > 0 or game > 0:
            lines.append("")
            lines.append("**Activity Today:**")
            if work > 0:
                lines.append(f"• Work: {work:.1f}h")
            if game > 0:
                lines.append(f"• Gaming: {game:.1f}h")

    return {
        "question": params.get("question", ""),
        "query_type": "status_query",
        "answer": "\n".join(lines),
        "confidence": 0.8, "evidence": evidence,
        "source_count": len(anomalies) + len(notable) + len(sessions),
    }


# Dispatcher
ANSWER_HANDLERS = {
    QT_IMPORTANCE: answer_importance,
    QT_TREND: answer_trend,
    QT_CORRELATION: answer_correlation,
    QT_TIMELINE: answer_timeline,
    QT_ANOMALY: answer_anomaly,
    QT_CAUSAL_CHAIN: answer_causal_chain,
    QT_STATUS: answer_status,
}


# ==========================================================================
# PUBLIC API
# ==========================================================================

def answer_question(db_conn, question: str) -> dict:
    """End-to-end: question → classification → retrieval → synthesis.

    Returns a dict with:
      - question, query_type, answer (text), confidence, evidence (list), source_count
    """
    query_type, params = plan_query(question)

    handler = ANSWER_HANDLERS.get(query_type)
    if not handler:
        return {
            "question": question, "query_type": "unknown",
            "answer": "I couldn't classify that question type.",
            "confidence": 0.2, "evidence": [], "source_count": 0,
        }

    try:
        result = handler(db_conn, params)
        return result
    except Exception as exc:
        log.warning("Query handler %s failed: %s", query_type, exc)
        return {
            "question": question, "query_type": query_type,
            "answer": f"Error answering: {exc}",
            "confidence": 0.1, "evidence": [], "source_count": 0,
        }


def export_query_result(result: dict, question: str) -> Path:
    """Save query result to JSON export file."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = EXPORT_DIR / f"query_{ts}.json"
    filename.write_text(json.dumps(result, indent=2))
    return filename


# ==========================================================================
# HELPERS
# ==========================================================================

def _row_to_dict(row) -> dict:
    """Convert sqlite3.Row to plain dict."""
    return {k: row[k] for k in row.keys()}
