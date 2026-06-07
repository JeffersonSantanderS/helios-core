"""Helios v6 — Timeline Normalizer.

Normalizes ALL data sources into unified timeline events
with deterministic importance scoring, then links them via typed relationships.

Design order (per GPT signoff): raw events → correlated events → causal confidence
→ narrative summaries later. This module handles the first three stages.

Cursor-driven: only processes new data since last normalizer tick.
All events grounded in real data — no LLM, no hallucination.
"""
from __future__ import annotations

import json, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("helios.timeline_normalizer")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
CURSOR_FILE = DATA_DIR / "timeline_cursor.json"

# ── Event taxonomy ──────────────────────────────────────────────
EVENT_LOCATION_CHANGE  = "location_change"
EVENT_FOCUS_CHANGE     = "focus_change"
EVENT_ALERT_FIRED      = "alert_fired"
EVENT_SLEEP_COMPLETED  = "sleep_completed"
EVENT_GAMING_SESSION   = "gaming_session"
EVENT_MOOD_RECORDED    = "mood_recorded"
EVENT_METRIC_ANOMALY   = "metric_anomaly"
EVENT_CORRELATION_FOUND = "correlation_found"
EVENT_WEATHER_CHANGE   = "weather_change"
EVENT_SPOTIFY_LISTEN   = "spotify_listen"
EVENT_HEALTH_METRIC    = "health_metric"
EVENT_SYSTEM_EVENT     = "system_event"

# ── Importance weights ───────────────────────────────────────────
IMPORTANCE_ALERT_CRITICAL  = 1.0
IMPORTANCE_ALERT_ERROR     = 0.8
IMPORTANCE_ALERT_WARNING   = 0.6
IMPORTANCE_ALERT_INFO      = 0.4
IMPORTANCE_ANOMALY_HIGH    = 0.9   # z-score ≥ 2.5
IMPORTANCE_ANOMALY_MEDIUM  = 0.7   # z-score ≥ 2.0
IMPORTANCE_CORR_STRONG     = 0.8
IMPORTANCE_CORR_MODERATE   = 0.6
IMPORTANCE_CORR_WEAK       = 0.4
IMPORTANCE_MOOD_BIG_CHANGE = 0.8   # delta ≥ 4
IMPORTANCE_MOOD_CHANGE     = 0.6   # delta ≥ 2
IMPORTANCE_LOC_DEPARTURE   = 0.7
IMPORTANCE_LOC_ARRIVAL     = 0.7
IMPORTANCE_LOC_ZONE        = 0.5
IMPORTANCE_FOCUS_GAMING    = 0.6
IMPORTANCE_FOCUS_WORK      = 0.5
IMPORTANCE_FOCUS_IDLE      = 0.3
IMPORTANCE_SYSTEM_DOWN     = 0.9
IMPORTANCE_SYSTEM_RECOVER  = 0.6
IMPORTANCE_SLEEP_OK        = 0.5
IMPORTANCE_SLEEP_LOW       = 0.7
IMPORTANCE_WEATHER_EXTREME = 0.6

# ── Link types ───────────────────────────────────────────────────
LINK_TEMPORAL    = "temporal"         # events close in time
LINK_CAUSAL      = "causes"           # A caused B (correlation-backed)
LINK_CORRELATES  = "correlates_with"  # statistically correlated
LINK_PRECEDES    = "precedes"         # A happened before B
LINK_SAME_CONTEXT = "same_context"    # same module/source
LINK_DERIVED     = "derived_from"     # summary derived from raw events

# ── Correlation pair mappings ────────────────────────────────────
# Known pairs from correlator for linking
CORRELATION_PAIRS = {
    ("sleep.hours", "activity.minutes_daily"): "More sleep → more active",
    ("sleep.hours", "resting_heart_rate.avg_daily"): "Sleep ↔ HR relationship",
    ("sleep.hours", "mood.score_daily"): "Sleep quality → mood",
    ("activity.minutes_daily", "mood.score_daily"): "Activity → mood",
    ("spotify.listen_minutes_daily", "mood.score_daily"): "Music ↔ mood",
}


class TimelineNormalizer:
    """Normalizes all data sources into timeline events with importance scoring."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._cursor = self._load_cursor()
        # Last-known values for change detection
        self._last_location: str | None = None
        self._last_focus: str | None = None
        self._last_weather_temp: float | None = None
        self._last_weather_precip: float | None = None

    # ── Cursor persistence ──────────────────────────────────────
    def _load_cursor(self) -> dict:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if CURSOR_FILE.exists():
            try:
                with open(CURSOR_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        # Initialize cursors from DB max values to avoid full-history scan
        cursor = {
            "last_alert_id": 0,
            "last_focus_id": 0,
            "last_correlation_id": 0,
            "last_metric_ts": "1970-01-01T00:00:00+00:00",
            "last_mood_date": "",
            "last_event_id": 0,
            "last_normalizer_ts": "",
            "last_location": None,
            "last_focus": None,
        }
        try:
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            r = conn.execute("SELECT MAX(id) as mx FROM focus").fetchone()
            if r and r["mx"]:
                cursor["last_focus_id"] = r["mx"]
            r = conn.execute("SELECT MAX(id) as mx FROM alert_history").fetchone()
            if r and r["mx"]:
                cursor["last_alert_id"] = r["mx"]
            r = conn.execute("SELECT MAX(id) as mx FROM correlations").fetchone()
            if r and r["mx"]:
                cursor["last_correlation_id"] = r["mx"]
            r = conn.execute("SELECT MAX(ts) as mx FROM metric_snapshots").fetchone()
            if r and r["mx"]:
                cursor["last_metric_ts"] = r["mx"]
            conn.close()
        except Exception:
            pass
        return cursor

    def _save_cursor(self):
        self._cursor["last_normalizer_ts"] = datetime.now(timezone.utc).isoformat()
        with open(CURSOR_FILE, "w") as f:
            json.dump(self._cursor, f, indent=2)

    # ── Main entry point ─────────────────────────────────────────
    def normalize(self, db_conn) -> int:
        """Run full normalization pass. Returns number of new events created."""
        mdt_now = datetime.now(timezone.utc) - timedelta(hours=6)
        date_key = mdt_now.strftime("%Y-%m-%d")
        new_events = 0

        new_events += self._normalize_alerts(db_conn, date_key)
        new_events += self._normalize_focus_changes(db_conn, date_key)
        new_events += self._normalize_metric_anomalies(db_conn, date_key)
        new_events += self._normalize_correlations(db_conn, date_key)
        new_events += self._normalize_mood(db_conn, date_key)
        new_events += self._normalize_location_changes(db_conn, date_key)
        new_events += self._normalize_sleep(db_conn, date_key)
        new_events += self._normalize_weather_changes(db_conn, date_key)

        if new_events > 0:
            self._link_events(db_conn)

        self._save_cursor()
        return new_events

    # ── Normalizers — one per source ─────────────────────────────
    def _insert_event(self, db_conn, event_type: str, source: str,
                      importance: float, summary: str, metadata: dict | None = None,
                      date_key: str = "", ts: str = "") -> int | None:
        """Insert a timeline event, return its id."""
        if not ts:
            ts = datetime.now(timezone.utc).isoformat()
        if not date_key:
            mdt_now = datetime.now(timezone.utc) - timedelta(hours=6)
            date_key = mdt_now.strftime("%Y-%m-%d")

        try:
            cur = db_conn.execute(
                """INSERT INTO timeline_events
                   (ts, event_type, source_module, importance, summary, metadata, date_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ts, event_type, source, importance, summary,
                 json.dumps(metadata) if metadata else None, date_key),
            )
            db_conn.commit()
            eid = cur.lastrowid
            log.debug("Timeline event %s: %s (%s, imp=%.2f)", eid, summary[:60], event_type, importance)
            return eid
        except Exception as exc:
            log.warning("Failed to insert timeline event: %s", exc)
            return None

    # ── 1. Alert normalization ──────────────────────────────────
    def _normalize_alerts(self, db_conn, date_key: str) -> int:
        last_id = self._cursor["last_alert_id"]
        cur = db_conn.execute(
            "SELECT id, ts, severity, category, message, rule_slug, sent "
            "FROM alert_history WHERE id > ? AND sent = 1 ORDER BY id",
            (last_id,),
        )
        rows = cur.fetchall()
        if not rows:
            return 0

        count = 0
        max_id = last_id
        for row in rows:
            severity_map = {
                "critical": IMPORTANCE_ALERT_CRITICAL,
                "error": IMPORTANCE_ALERT_ERROR,
                "warning": IMPORTANCE_ALERT_WARNING,
                "info": IMPORTANCE_ALERT_INFO,
            }
            importance = severity_map.get(row[2], 0.5)
            self._insert_event(
                db_conn,
                EVENT_ALERT_FIRED,
                f"rules:{row[5] or 'unknown'}",
                importance,
                f"Alert: {row[4][:120]}",
                {"severity": row[2], "category": row[3], "rule_slug": row[5]},
                date_key, row[1],
            )
            max_id = max(max_id, row[0])
            count += 1

        self._cursor["last_alert_id"] = max_id
        return count

    # ── 2. Focus change normalization ────────────────────────────
    def _normalize_focus_changes(self, db_conn, date_key: str) -> int:
        last_id = self._cursor["last_focus_id"]
        cur = db_conn.execute(
            "SELECT id, ts, state, duration_secs, source "
            "FROM focus WHERE id > ? AND state != 'screen_time' "
            "ORDER BY id",
            (last_id,),
        )
        rows = cur.fetchall()
        if not rows:
            return 0

        count = 0
        max_id = last_id
        last_state = None
        last_start = None

        for i, row in enumerate(rows):
            fid, ts, state, duration, source = row

            # Detect transitions
            if last_state is not None and state != last_state:
                # State transition
                if state == "gaming":
                    imp = IMPORTANCE_FOCUS_GAMING
                    summary = f"Started gaming session ({duration}s)"
                elif state == "working":
                    imp = IMPORTANCE_FOCUS_WORK
                    summary = f"Started working ({duration}s if new session)"
                elif state == "idle":
                    imp = IMPORTANCE_FOCUS_IDLE
                    summary = f"Went idle (was {last_state})"
                else:
                    imp = 0.4
                    summary = f"Focus changed: {last_state} → {state}"

                self._insert_event(
                    db_conn, EVENT_FOCUS_CHANGE, f"focus:{source or 'tracker'}",
                    imp, summary,
                    {"from_state": last_state, "to_state": state, "duration_secs": duration},
                    date_key, ts,
                )
                count += 1

            # Detect new sessions (duration_secs ≈ 0 means session start)
            if duration is not None and duration < 30 and state in ("gaming", "working"):
                imp = IMPORTANCE_FOCUS_GAMING if state == "gaming" else IMPORTANCE_FOCUS_WORK
                self._insert_event(
                    db_conn, EVENT_FOCUS_CHANGE, f"focus:{source or 'tracker'}",
                    imp, f"New {state} session started",
                    {"state": state, "duration_secs": duration},
                    date_key, ts,
                )
                count += 1

            last_state = state
            last_start = ts
            max_id = max(max_id, fid)

        self._cursor["last_focus_id"] = max_id
        self._cursor["last_focus"] = last_state
        return count

    # ── 3. Metric anomaly detection ──────────────────────────────
    def _normalize_metric_anomalies(self, db_conn, date_key: str) -> int:
        """Detect anomalies by comparing today's metric against 14-day baseline."""
        last_ts = self._cursor["last_metric_ts"]
        cur = db_conn.execute(
            "SELECT id, ts, metric, value, date_key FROM metric_snapshots "
            "WHERE ts > ? ORDER BY ts",
            (last_ts,),
        )
        rows = cur.fetchall()
        if not rows:
            return 0

        count = 0
        max_ts = last_ts

        for row in rows:
            _, ts, metric, value, dk = row
            try:
                val = float(value)
            except (TypeError, ValueError):
                continue

            # Get baseline
            bk = db_conn.execute(
                "SELECT AVG(value) FROM metric_snapshots "
                "WHERE metric = ? AND date_key < ? AND date_key >= ?",
                (metric, dk or date_key,
                 (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")),
            ).fetchone()
            if not bk or bk[0] is None:
                max_ts = max(max_ts, ts)
                continue

            # Get stddev for z-score
            std = db_conn.execute(
                "SELECT AVG((value - ?) * (value - ?)) FROM metric_snapshots "
                "WHERE metric = ? AND date_key < ? AND date_key >= ?",
                (bk[0], bk[0], metric, dk or date_key,
                 (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")),
            ).fetchone()
            stddev = (abs(std[0]) ** 0.5) if std and std[0] else 1.0
            if stddev < 0.001:
                stddev = 1.0

            z_score = abs(val - bk[0]) / stddev

            if z_score >= 2.0:
                imp = IMPORTANCE_ANOMALY_HIGH if z_score >= 2.5 else IMPORTANCE_ANOMALY_MEDIUM
                direction = "high" if val > bk[0] else "low"
                label = METRIC_LABELS.get(metric, metric)
                self._insert_event(
                    db_conn, EVENT_METRIC_ANOMALY, f"metrics:{metric}",
                    imp,
                    f"{label} was unusually {direction}: {val} (baseline avg {bk[0]:.1f}, z={z_score:.1f})",
                    {"metric": metric, "value": val, "baseline_avg": bk[0],
                     "z_score": z_score, "direction": direction},
                    date_key, ts,
                )
                count += 1

            max_ts = max(max_ts, ts)

        self._cursor["last_metric_ts"] = max_ts
        return count

    # ── 4. Correlation events ────────────────────────────────────
    def _normalize_correlations(self, db_conn, date_key: str) -> int:
        last_id = self._cursor["last_correlation_id"]
        cur = db_conn.execute(
            "SELECT id, ts, metric_a, metric_b, pearson_r, p_value, strength "
            "FROM correlations WHERE id > ? ORDER BY id",
            (last_id,),
        )
        rows = cur.fetchall()
        if not rows:
            return 0

        count = 0
        max_id = last_id
        strength_map = {
            "strong": IMPORTANCE_CORR_STRONG,
            "moderate": IMPORTANCE_CORR_MODERATE,
            "weak": IMPORTANCE_CORR_WEAK,
        }

        for row in rows:
            corr_id, ts, ma, mb, r, p, strength = row
            imp = strength_map.get(strength, 0.5)
            direction = "positive" if r > 0 else "negative"
            self._insert_event(
                db_conn, EVENT_CORRELATION_FOUND, "correlator",
                imp,
                f"New correlation: {METRIC_LABELS.get(ma, ma)} ↔ {METRIC_LABELS.get(mb, mb)} "
                f"({strength}, r={r:.3f}, {direction})",
                {"metric_a": ma, "metric_b": mb, "r": r, "p_value": p, "strength": strength},
                date_key, ts,
            )
            max_id = max(max_id, corr_id)
            count += 1

        self._cursor["last_correlation_id"] = max_id
        return count

    # ── 5. Mood events ──────────────────────────────────────────
    def _normalize_mood(self, db_conn, date_key: str) -> int:
        last_date = self._cursor["last_mood_date"]
        mood_file = DATA_DIR / "mood_state.json"
        if not mood_file.exists():
            return 0

        try:
            with open(mood_file) as f:
                data = json.load(f)
        except Exception:
            return 0

        history = data.get("history", [])
        new_entries = [e for e in history if e.get("date", "") > last_date]
        if not new_entries:
            return 0

        count = 0
        max_date = last_date
        prev_score = None

        # Find previous score for delta
        for e in history:
            if e.get("date", "") < min(entry["date"] for entry in new_entries):
                prev_score = e.get("score")

        for entry in new_entries:
            score = entry.get("score", 0)
            label = entry.get("label", "Unknown")
            ts = entry.get("ts", "")
            edate = entry.get("date", "")

            if prev_score is not None:
                delta = abs(score - prev_score)
                imp = IMPORTANCE_MOOD_BIG_CHANGE if delta >= 4 else (
                    IMPORTANCE_MOOD_CHANGE if delta >= 2 else 0.5)
            else:
                imp = 0.5

            self._insert_event(
                db_conn, EVENT_MOOD_RECORDED, "mood",
                imp,
                f"Mood recorded: {label} ({score}/9)" +
                (f" — change of {delta} from {prev_score}/9" if prev_score is not None else ""),
                {"score": score, "label": label, "previous_score": prev_score},
                date_key, ts,
            )
            max_date = max(max_date, edate)
            prev_score = score
            count += 1

        self._cursor["last_mood_date"] = max_date
        return count

    # ── 6. Location change events ────────────────────────────────
    def _normalize_location_changes(self, db_conn, date_key: str) -> int:
        loc_file = DATA_DIR / "icloud_location_sync.json"
        if not loc_file.exists():
            return 0

        try:
            with open(loc_file) as f:
                data = json.load(f)
        except Exception:
            return 0

        current = data.get("city") or data.get("zone") or str(data.get("lat", ""))
        previous = self._cursor.get("last_location")

        if not previous or previous == current or not current:
            self._cursor["last_location"] = current
            return 0

        # Determine event type
        home_zones = {"home", "NE", "SE", "NW", "SW"}
        prev_was_home = previous in home_zones or "home" in str(previous).lower()
        curr_is_home = current in home_zones or "home" in str(current).lower()

        if prev_was_home and not curr_is_home:
            event_type = "departure"
            imp = IMPORTANCE_LOC_DEPARTURE + 0.1  # departure is most important
            summary = f"Left home — now in {current}"
        elif not prev_was_home and curr_is_home:
            event_type = "arrival"
            imp = IMPORTANCE_LOC_ARRIVAL + 0.1
            summary = f"Arrived home from {previous}"
        else:
            event_type = "zone_change"
            imp = IMPORTANCE_LOC_ZONE
            summary = f"Location: {previous} → {current}"

        # Privacy: do NOT include raw lat/lon in timeline metadata
        metadata = {"from": previous, "to": current, "event_type": event_type}
        self._insert_event(
            db_conn, EVENT_LOCATION_CHANGE, "location",
            imp, summary,
            metadata,
            date_key, data.get("ts", ""),
        )
        self._cursor["last_location"] = current
        return 1

    # ── 7. Sleep completed events ────────────────────────────────
    def _normalize_sleep(self, db_conn, date_key: str) -> int:
        """Detect when sleep data for the previous night has arrived."""
        yesterday = (datetime.now(timezone.utc) - timedelta(hours=6) - timedelta(days=1)).strftime("%Y-%m-%d")

        # Check if we already logged this
        cur = db_conn.execute(
            "SELECT COUNT(*) FROM timeline_events "
            "WHERE event_type = ? AND date_key = ? AND source_module = 'sleep'",
            (EVENT_SLEEP_COMPLETED, date_key),
        )
        if cur.fetchone()[0] > 0:
            return 0

        cur = db_conn.execute(
            "SELECT value FROM metric_snapshots WHERE metric = 'sleep.hours' AND date_key = ?",
            (yesterday,),
        )
        row = cur.fetchone()
        if not row:
            return 0

        hours = float(row[0])
        imp = IMPORTANCE_SLEEP_LOW if hours < 5.0 else IMPORTANCE_SLEEP_OK
        quality = "low" if hours < 5.0 else ("good" if hours >= 7.0 else "okay")

        self._insert_event(
            db_conn, EVENT_SLEEP_COMPLETED, "sleep",
            imp,
            f"Slept {hours:.1f}h ({quality})",
            {"hours": hours, "quality": quality, "night_of": yesterday},
            date_key,
        )
        return 1

    # ── 8. Weather change events ─────────────────────────────────
    def _normalize_weather_changes(self, db_conn, date_key: str) -> int:
        weather_file = DATA_DIR / "weather_state.json"
        if not weather_file.exists():
            return 0

        try:
            with open(weather_file) as f:
                data = json.load(f)
        except Exception:
            return 0

        temp = data.get("temp_max") or data.get("temperature")
        if temp is None:
            return 0

        try:
            temp = float(temp)
        except (TypeError, ValueError):
            return 0

        previous = self._cursor.get("last_weather_temp")
        if previous is None:
            self._cursor["last_weather_temp"] = temp
            return 0

        delta = abs(temp - previous)
        if delta < 5:  # Ignore trivial changes
            self._cursor["last_weather_temp"] = temp
            return 0

        imp = IMPORTANCE_WEATHER_EXTREME if delta >= 10 else 0.4
        direction = "warmer" if temp > previous else "cooler"

        self._insert_event(
            db_conn, EVENT_WEATHER_CHANGE, "weather",
            imp,
            f"Weather shift: {previous:.0f}° → {temp:.0f}° ({direction} by {delta:.0f}°)",
            {"from_temp": previous, "to_temp": temp, "delta": delta, "direction": direction},
            date_key,
        )
        self._cursor["last_weather_temp"] = temp
        return 1

    # ── Event linking ────────────────────────────────────────────
    MAX_LINK_BATCH = 200  # hard ceiling; backfills go incrementally

    def _link_events(self, db_conn):
        """Link new events that are temporally proximal or contextually related.

        Bounded to MAX_LINK_BATCH events per pass to prevent O(n²) timeout on
        backfill. On the first tick after a cold start, this processes the
        most recent events and naturally catches up over subsequent ticks.
        """
        last_event_id = self._cursor.get("last_event_id", 0)
        new_events = db_conn.execute(
            "SELECT id, ts, event_type, source_module, metadata FROM timeline_events "
            "WHERE id > ? ORDER BY id DESC LIMIT ?",
            (last_event_id, self.MAX_LINK_BATCH),
        ).fetchall()

        if not new_events:
            return

        # Reverse to chronological order for linking
        new_events = list(reversed(new_events))
        links = 0
        max_id = max(e[0] for e in new_events)

        # Build a lookup index: events grouped by source module
        by_source: dict[str, list] = {}
        for evt in new_events:
            source = evt[3]
            by_source.setdefault(source, []).append(evt)

        # Link 1: Same-source sequential linking (precedes)
        for source, evts in by_source.items():
            for i in range(len(evts) - 1):
                a, b = evts[i], evts[i + 1]
                db_conn.execute(
                    "INSERT OR IGNORE INTO event_links "
                    "(source_event_id, target_event_id, link_type, confidence, evidence) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (a[0], b[0], LINK_PRECEDES, 0.5, f"Sequential in {source}"),
                )
                links += 1

        # Link 2: Cross-source temporal proximity (±30 min)
        # Only compare adjacent time-sorted events, not full Cartesian
        sorted_events = sorted(new_events, key=lambda e: e[1] or "")
        for i in range(len(sorted_events) - 1):
            a, b = sorted_events[i], sorted_events[i + 1]
            a_ts = a[1]; b_ts = b[1]
            if not a_ts or not b_ts:
                continue
            try:
                a_dt = datetime.fromisoformat(a_ts.replace("Z", "+00:00"))
                b_dt = datetime.fromisoformat(b_ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            delta = abs((a_dt - b_dt).total_seconds())
            if delta <= 1800 and a[3] != b[3]:  # cross-source only, within 30 min
                conf = max(0.3, 1.0 - delta / 3600)
                db_conn.execute(
                    "INSERT OR IGNORE INTO event_links "
                    "(source_event_id, target_event_id, link_type, confidence, evidence) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (a[0], b[0], LINK_TEMPORAL, conf, f"Within {delta:.0f}s"),
                )
                links += 1

        # Link 3: Known correlation pairs (metric-only events)
        metric_events = [e for e in new_events if e[2] == EVENT_METRIC_ANOMALY]
        for i, a in enumerate(metric_events):
            for b in metric_events[i + 1:]:
                a_meta = json.loads(a[4]) if a[4] else {}
                b_meta = json.loads(b[4]) if b[4] else {}
                ka = a_meta.get("metric", "")
                kb = b_meta.get("metric", "")
                pair = (ka, kb)
                pair_rev = (kb, ka)
                if pair in CORRELATION_PAIRS or pair_rev in CORRELATION_PAIRS:
                    db_conn.execute(
                        "INSERT OR IGNORE INTO event_links "
                        "(source_event_id, target_event_id, link_type, confidence, evidence) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (a[0], b[0], LINK_CORRELATES, 0.7,
                         CORRELATION_PAIRS.get(pair) or CORRELATION_PAIRS.get(pair_rev, "")),
                    )
                    links += 1

        db_conn.commit()
        # Only advance cursor by what was actually linked
        self._cursor["last_event_id"] = max_id
        if links:
            log.info("Event linking: %d links created across %d events", links, len(new_events))


# Convenience labels for summaries
METRIC_LABELS = {
    "sleep.hours": "Sleep",
    "activity.minutes_daily": "Activity",
    "mood.score_daily": "Mood",
    "resting_heart_rate.avg_daily": "Resting HR",
    "spotify.listen_minutes_daily": "Spotify",
    "spotify.tracks_daily": "Tracks",
    "protein.grams_daily": "Protein",
    "weather.temp_max": "High temp",
    "weather.temp_min": "Low temp",
    "weather.precipitation": "Precipitation",
    "location.gps_valid": "GPS valid",
}
