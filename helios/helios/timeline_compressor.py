"""Helios v6 — Timeline Compressor.

Collapses raw timeline_events into operational sessions,
scores by salience (importance x novelty x confidence), and extracts
notable events deterministically. No LLM.

Pipeline:
  raw timeline_events -> grouped sessions -> compressed windows ->
  salience scoring -> notable-event extraction -> (future) narrative layer
"""
from __future__ import annotations

import json, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("helios.timeline_compressor")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
CURSOR_FILE = DATA_DIR / "compressor_cursor.json"

# -- Session types --
SESSION_FOCUS_BLOCK   = "focus_block"
SESSION_SLEEP_PERIOD  = "sleep_period"
SESSION_MOOD_LOG      = "mood_log"
SESSION_LOCATION_TRIP = "location_trip"
SESSION_ALERT_CLUSTER = "alert_cluster"

# -- Salience weights (composite = imp*alpha + nov*beta + conf*gamma) --
SALIENCE_IMPORTANCE_WEIGHT = 0.45
SALIENCE_NOVELTY_WEIGHT    = 0.35
SALIENCE_CONFIDENCE_WEIGHT = 0.20

# -- Thresholds --
DUP_WINDOW_SECS        = 300   # merge sessions within 5 min
NOTABLE_MIN_IMPORTANCE  = 0.55
NOTABLE_MIN_NOVELTY     = 0.4
MAX_NOTABLE_PER_DAY     = 10
MAX_FOCUS_SESSION_H     = 16    # split focus sessions longer than this

# -- Alert cluster caps --
ALERT_IMPORTANCE_CAP    = 0.85  # alerts can't exceed this (reserve 0.9+ for anomalies)

# -- Baseline references for novelty --
NORMAL_WORK_HOURS_PER_DAY   = 8.0
NORMAL_GAMING_HOURS_PER_DAY = 1.5
NORMAL_SLEEP_HOURS          = 7.0
NORMAL_LOCATION_CHANGES     = 2

# -- Correlation pair mappings --
CORRELATION_PAIRS = {
    ("sleep.hours", "activity.minutes_daily"): "Sleep-Activity link",
    ("sleep.hours", "resting_heart_rate.avg_daily"): "Sleep-HR link",
    ("sleep.hours", "mood.score_daily"): "Sleep-Mood link",
    ("activity.minutes_daily", "mood.score_daily"): "Activity-Mood link",
}


class TimelineCompressor:
    """Compresses timeline events -> sessions, scores salience, extracts notables."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._cursor = self._load_cursor()

    # ==================================================================
    # CURSOR PERSISTENCE
    # ==================================================================
    def _load_cursor(self) -> dict:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if CURSOR_FILE.exists():
            try:
                with open(CURSOR_FILE) as f:
                    return json.load(f)
            except Exception:
                pass

        # First-run: seed from DB max event IDs so we only process new data
        cursor = {
            "last_focus_event_id": 0,
            "last_alert_event_id": 0,
            "last_sleep_event_id": 0,
            "last_location_event_id": 0,
            "last_notable_date": "",
            "last_compressor_ts": "",
        }
        try:
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            for key, etype in [
                ("last_focus_event_id", "focus_change"),
                ("last_alert_event_id", "alert_fired"),
                ("last_sleep_event_id", "sleep_completed"),
                ("last_location_event_id", "location_change"),
            ]:
                r = conn.execute(
                    "SELECT MAX(id) FROM timeline_events WHERE event_type=?",
                    (etype,),
                ).fetchone()
                if r and r[0]:
                    cursor[key] = r[0]
            conn.close()
        except Exception:
            pass
        return cursor

    def _save_cursor(self):
        self._cursor["last_compressor_ts"] = datetime.now(timezone.utc).isoformat()
        with open(CURSOR_FILE, "w") as f:
            json.dump(self._cursor, f, indent=2)

    # ==================================================================
    # MAIN ENTRY POINT
    # ==================================================================
    def compress(self, db_conn) -> int:
        mdt_now = datetime.now(timezone.utc) - timedelta(hours=6)
        date_key = mdt_now.strftime("%Y-%m-%d")

        sessions_created = 0

        # 1. Compress focus_change events into contiguous-state sessions
        sessions_created += self._compress_focus(db_conn, date_key)

        # 2. Promote non-focus events to session level
        sessions_created += self._promote_sleep(db_conn, date_key)
        sessions_created += self._promote_locations(db_conn, date_key)
        sessions_created += self._promote_alerts(db_conn, date_key)

        # 3. Score salience (importance + novelty + confidence)
        self._score_salience(db_conn)

        # 4. Collapse duplicate sessions
        self._collapse_duplicates(db_conn)

        # 5. Extract daily notable events
        self._extract_notable_events(db_conn, date_key)

        self._save_cursor()
        return sessions_created

    # ==================================================================
    # 1. FOCUS SESSION COMPRESSION
    # ==================================================================
    def _compress_focus(self, db_conn, date_key: str) -> int:
        last_eid = self._cursor["last_focus_event_id"]
        events = db_conn.execute(
            "SELECT id, ts, metadata FROM timeline_events "
            "WHERE event_type='focus_change' AND id > ? ORDER BY ts",
            (last_eid,),
        ).fetchall()
        if not events:
            return 0

        # Group into contiguous same-state stretches
        sessions: list[dict] = []
        cur = None
        for evt in events:
            eid, ts, meta_str = evt
            meta = json.loads(meta_str) if meta_str else {}
            state = meta.get("to_state") or meta.get("state") or ""
            if state in ("screen_time", "", "unknown"):
                continue
            ts_dt = self._parse_ts(ts)

            if cur is None:
                cur = {"start": ts, "end": ts, "s_dt": ts_dt,
                       "e_dt": ts_dt, "st": state, "eids": [eid]}
            elif state == cur["st"]:
                cur["end"] = ts; cur["e_dt"] = ts_dt; cur["eids"].append(eid)
            else:
                sessions.append(cur)
                cur = {"start": ts, "end": ts, "s_dt": ts_dt,
                       "e_dt": ts_dt, "st": state, "eids": [eid]}
        if cur is not None:
            sessions.append(cur)

        # Insert sessions, splitting at date boundaries
        count = 0
        labels = {"working": "Worked", "gaming": "Gaming session",
                  "idle": "Idle period", "meeting": "Meeting", "break": "Break"}

        for sess in sessions:
            for chunk in self._split_session(sess):
                dur = (chunk["e_dt"] - chunk["s_dt"]).total_seconds()
                if dur < 30:
                    continue
                label = labels.get(chunk["st"], chunk["st"].capitalize())
                summary = self._fmt_session(label, chunk["s_dt"], chunk["e_dt"], dur)

                self._insert_session(
                    db_conn, SESSION_FOCUS_BLOCK, date_key,
                    chunk["s_dt"].isoformat(), chunk["e_dt"].isoformat(), dur,
                    chunk["st"], len(sess["eids"]), sess["eids"], summary, {},
                )
                count += 1

        if count:
            log.info("Compressor: %d focus sessions from %d events", count, len(events))
        return count

    # ==================================================================
    # 2. PROMOTE NON-FOCUS EVENTS
    # ==================================================================
    def _promote_sleep(self, db_conn, date_key: str) -> int:
        last_eid = self._cursor["last_sleep_event_id"]
        events = db_conn.execute(
            "SELECT id, ts, metadata, summary FROM timeline_events "
            "WHERE event_type='sleep_completed' AND id > ?",
            (last_eid,),
        ).fetchall()
        if not events:
            return 0
        count = 0
        for evt in events:
            eid, ts, meta_str, summary = evt
            meta = json.loads(meta_str) if meta_str else {}
            hours = meta.get("hours", 0)
            end_dt = self._parse_ts(ts)
            start_dt = end_dt - timedelta(hours=hours)
            self._insert_session(
                db_conn, SESSION_SLEEP_PERIOD, date_key,
                start_dt.isoformat(), ts, hours * 3600,
                "asleep", 1, [eid], summary, meta,
            )
            count += 1
        return count

    def _promote_locations(self, db_conn, date_key: str) -> int:
        last_eid = self._cursor["last_location_event_id"]
        events = db_conn.execute(
            "SELECT id, ts, metadata, summary FROM timeline_events "
            "WHERE event_type='location_change' AND id > ? ORDER BY ts",
            (last_eid,),
        ).fetchall()
        if not events:
            return 0

        dep = None; count = 0
        for evt in events:
            eid, ts, meta_str, summary = evt
            meta = json.loads(meta_str) if meta_str else {}
            etype = meta.get("event_type", "")
            if etype == "departure":
                dep = (eid, ts, meta, summary)
            elif etype == "arrival" and dep is not None:
                deid, dts, dmeta, dsummary = dep
                dur = (self._parse_ts(ts) - self._parse_ts(dts)).total_seconds()
                self._insert_session(
                    db_conn, SESSION_LOCATION_TRIP, date_key,
                    dts, ts, dur, "out_of_home", 2, [deid, eid],
                    f"Trip: {dmeta.get('to','?')} ({self._fmt_dur(dur)})",
                    {"from": dmeta.get("from"), "to": meta.get("to")},
                )
                count += 1; dep = None
        return count

    def _promote_alerts(self, db_conn, date_key: str) -> int:
        """Group identical alerts into count clusters; cross-type bursts separately."""
        last_eid = self._cursor["last_alert_event_id"]
        events = db_conn.execute(
            "SELECT id, ts, metadata, summary FROM timeline_events "
            "WHERE event_type='alert_fired' AND id > ? ORDER BY ts",
            (last_eid,),
        ).fetchall()
        if not events:
            return 0

        # Step A: group by fingerprint (first 80 chars of message)
        by_fp: dict[str, list] = {}
        for evt in events:
            fp = evt[3][:80]
            by_fp.setdefault(fp, []).append(evt)

        count = 0
        for fp, evts in by_fp.items():
            if len(evts) < 3:   # need 3+ identical alerts to cluster
                continue
            eids = [e[0] for e in evts]
            dur = (self._parse_ts(evts[-1][1]) - self._parse_ts(evts[0][1])).total_seconds()
            meta0 = json.loads(evts[0][2]) if evts[0][2] else {}
            self._insert_session(
                db_conn, SESSION_ALERT_CLUSTER, date_key,
                evts[0][1], evts[-1][1], max(dur, 1),
                "alerting", len(evts), eids,
                f"Alert cluster ({len(evts)}x): {fp}",
                {"alert_count": len(evts), "severity": meta0.get("severity","info"),
                 "fingerprint": fp[:60]},
            )
            count += 1

        # Step B: cross-fingerprint temporal bursts (different alerts < 5 min apart)
        if len(events) >= 4:
            sorted_evts = sorted(events, key=lambda e: e[1])
            burst = [sorted_evts[0]]; burst_count = 0
            for i in range(1, len(sorted_evts)):
                delta = (self._parse_ts(sorted_evts[i][1]) -
                         self._parse_ts(sorted_evts[i-1][1])).total_seconds()
                if delta <= 300:
                    burst.append(sorted_evts[i])
                else:
                    burst_count += self._maybe_burst(db_conn, burst, date_key)
                    burst = [sorted_evts[i]]
            burst_count += self._maybe_burst(db_conn, burst, date_key)
            count += burst_count

        return count

    def _maybe_burst(self, db_conn, burst: list, date_key: str) -> int:
        if len(burst) < 4:
            return 0
        fps = set(e[3][:80] for e in burst)
        if len(fps) < 2:
            return 0
        eids = [e[0] for e in burst]
        dur = (self._parse_ts(burst[-1][1]) - self._parse_ts(burst[0][1])).total_seconds()
        self._insert_session(
            db_conn, SESSION_ALERT_CLUSTER, date_key,
            burst[0][1], burst[-1][1], max(dur, 1),
            "alerting", len(burst), eids,
            f"Alert burst: {len(burst)} alerts across {len(fps)} types",
            {"alert_count": len(burst), "type_count": len(fps)},
        )
        return 1

    # ==================================================================
    # 3. SALIENCE SCORING
    # ==================================================================
    def _score_salience(self, db_conn):
        """Score all unscored sessions (importance = 0 or NULL)."""
        sessions = db_conn.execute(
            "SELECT id, session_type, dominant_state, duration_secs, "
            "event_count, date_key FROM timeline_sessions "
            "WHERE importance IS NULL OR importance = 0.0"
        ).fetchall()
        if not sessions:
            return

        daily_stats: dict[str, dict] = {}
        for dk in set(s[5] for s in sessions):
            daily_stats[dk] = self._daily_stats(db_conn, dk)

        for sid, stype, state, dur, ec, dk in sessions:
            imp = self._score_importance(stype, state, dur, ec)
            nov = self._score_novelty(stype, state, dur, dk, daily_stats.get(dk, {}))
            con = self._score_confidence(stype, ec, dur)

            # Cap alert importance
            if stype == SESSION_ALERT_CLUSTER:
                imp = min(imp, ALERT_IMPORTANCE_CAP)

            db_conn.execute(
                "UPDATE timeline_sessions SET importance=?, novelty=?, confidence=? "
                "WHERE id=?",
                (round(imp, 3), round(nov, 3), round(con, 3), sid),
            )
        db_conn.commit()
        log.debug("Salience scored for %d sessions", len(sessions))

    def _score_importance(self, stype: str, state: str, dur: float, ec: int) -> float:
        base = 0.4
        base += {"focus_block": 0.1, "sleep_period": 0.2,
                 "location_trip": 0.2, "alert_cluster": 0.25,
                 "mood_log": 0.15}.get(stype, 0)
        base += {True: 0.15, False: 0.1}.get(dur > 7200, 0.05 if dur > 3600 else 0)
        base += {"gaming": 0.1, "working": 0.05, "asleep": 0.1,
                 "out_of_home": 0.1, "alerting": 0.05}.get(state, 0)
        return min(1.0, base)

    def _score_novelty(self, stype: str, state: str, dur: float,
                       dk: str, stats: dict) -> float:
        dur_h = dur / 3600
        if stype == "focus_block":
            if state == "working":
                tw = stats.get("work_h", NORMAL_WORK_HOURS_PER_DAY)
                return min(0.9, 0.3 + abs(dur_h - tw) / tw)
            if state == "gaming":
                return (0.9 if dur_h > NORMAL_GAMING_HOURS_PER_DAY * 2 else
                        0.7 if dur_h > NORMAL_GAMING_HOURS_PER_DAY * 1.5 else
                        0.5 if dur_h > NORMAL_GAMING_HOURS_PER_DAY else 0.2)
            if state == "idle" and dur_h > 1:
                return 0.6
            return 0.3
        if stype == "sleep_period":
            return min(0.9, 0.3 + abs(dur_h - NORMAL_SLEEP_HOURS) / NORMAL_SLEEP_HOURS)
        if stype == "location_trip":
            trips = stats.get("trips", 1)
            return 0.9 if trips > NORMAL_LOCATION_CHANGES * 2 else (
                   0.6 if trips > NORMAL_LOCATION_CHANGES else 0.4)
        if stype == "alert_cluster":
            return 0.65
        return 0.3

    def _score_confidence(self, stype: str, ec: int, dur: float) -> float:
        conf = 0.9 if ec >= 20 else 0.8 if ec >= 10 else \
               0.7 if ec >= 5 else 0.6 if ec >= 2 else 0.5
        if dur > 3600 and ec < 3:
            conf = max(0.3, conf - 0.2)
        return conf

    def _daily_stats(self, db_conn, dk: str) -> dict:
        r = db_conn.execute(
            "SELECT total_secs FROM focus_daily_summary "
            "WHERE date_key=? AND state='working'", (dk,),
        ).fetchone()
        work_h = float(r[0]) / 3600 if r else NORMAL_WORK_HOURS_PER_DAY
        r = db_conn.execute(
            "SELECT COUNT(*) FROM timeline_sessions "
            "WHERE session_type='location_trip' AND date_key=?", (dk,),
        ).fetchone()
        trips = r[0] if r else 0
        return {"work_h": work_h, "trips": trips}

    # ==================================================================
    # 4. DUPLICATE COLLAPSE
    # ==================================================================
    def _collapse_duplicates(self, db_conn):
        sessions = db_conn.execute(
            "SELECT id, session_type, session_start, summary, event_count, source_events "
            "FROM timeline_sessions "
            "WHERE importance IS NULL OR importance = 0.0 OR "
            "(importance > 0 AND novelty > 0) "
            "ORDER BY session_start"
        ).fetchall()
        if len(sessions) < 2:
            return
        merged = 0; i = 0
        while i < len(sessions) - 1:
            a, b = sessions[i], sessions[i + 1]
            delta = abs((self._parse_ts(a[2]) - self._parse_ts(b[2])).total_seconds())
            if (a[1] == b[1] and delta <= DUP_WINDOW_SECS
                    and self._similar(a[3], b[3])):
                a_evts = set(json.loads(a[5]) if a[5] else [])
                b_evts = set(json.loads(b[5]) if b[5] else [])
                merged_evts = list(a_evts | b_evts)
                db_conn.execute(
                    "UPDATE timeline_sessions SET event_count=?, source_events=? "
                    "WHERE id=?",
                    (len(merged_evts), json.dumps(merged_evts), a[0]),
                )
                db_conn.execute(
                    "UPDATE notable_events SET session_id=? WHERE session_id=?",
                    (a[0], b[0]),
                )
                db_conn.execute("DELETE FROM timeline_sessions WHERE id=?", (b[0],))
                sessions.pop(i + 1); merged += 1
            else:
                i += 1
        db_conn.commit()
        if merged:
            log.info("Compressor: merged %d duplicate sessions", merged)

    # ==================================================================
    # 5. NOTABLE EVENT EXTRACTION
    # ==================================================================
    def _extract_notable_events(self, db_conn, date_key: str):
        if date_key <= self._cursor["last_notable_date"]:
            return

        candidates: list[dict] = []

        # A: Top anomalies
        for r in db_conn.execute(
            "SELECT id, ts, summary, importance, metadata FROM timeline_events "
            "WHERE event_type='metric_anomaly' AND date_key=? "
            "ORDER BY importance DESC LIMIT 5", (date_key,),
        ).fetchall():
            meta = json.loads(r[4]) if r[4] else {}
            candidates.append({
                "type": "top_anomaly", "teid": r[0],
                "summary": r[2], "imp": r[3],
                "nov": min(0.95, 0.4 + meta.get("z_score", 0) / 4),
                "con": 0.85,
            })

        # B: Top correlations
        for r in db_conn.execute(
            "SELECT id, ts, summary, importance FROM timeline_events "
            "WHERE event_type='correlation_found' AND date_key=? "
            "ORDER BY importance DESC LIMIT 3", (date_key,),
        ).fetchall():
            candidates.append({
                "type": "top_correlation", "teid": r[0],
                "summary": r[2], "imp": r[3], "nov": 0.55, "con": 0.85,
            })

        # C: Top sessions by composite salience
        for r in db_conn.execute(
            "SELECT id, summary, importance, novelty, confidence, session_type "
            "FROM timeline_sessions WHERE date_key=? AND importance IS NOT NULL "
            "ORDER BY (importance * 0.45 + COALESCE(novelty,0.3) * 0.35 "
            "+ COALESCE(confidence,0.5) * 0.20) DESC LIMIT 7",
            (date_key,),
        ).fetchall():
            candidates.append({
                "type": "top_session", "sid": r[0],
                "summary": r[1], "imp": r[2], "nov": r[3] or 0.3, "con": r[4] or 0.5,
            })

        # D: Top mood shifts
        for r in db_conn.execute(
            "SELECT id, ts, summary, importance FROM timeline_events "
            "WHERE event_type='mood_recorded' AND date_key=? "
            "ORDER BY importance DESC LIMIT 2", (date_key,),
        ).fetchall():
            candidates.append({
                "type": "top_mood_shift", "teid": r[0],
                "summary": r[2], "imp": r[3], "nov": 0.5, "con": 0.9,
            })

        # Filter & rank
        for c in candidates:
            c["salience"] = (
                c["imp"] * SALIENCE_IMPORTANCE_WEIGHT +
                c.get("nov", 0.3) * SALIENCE_NOVELTY_WEIGHT +
                c.get("con", 0.5) * SALIENCE_CONFIDENCE_WEIGHT
            )
        candidates = [c for c in candidates
                      if c["imp"] >= NOTABLE_MIN_IMPORTANCE
                      or c.get("nov", 0) >= NOTABLE_MIN_NOVELTY]

        # Deduplicate by summary similarity, then sort
        seen = set(); deduped = []
        for c in sorted(candidates, key=lambda x: x["salience"], reverse=True):
            key = c["summary"][:60]
            if key not in seen:
                seen.add(key); deduped.append(c)

        for rank, c in enumerate(deduped[:MAX_NOTABLE_PER_DAY], 1):
            db_conn.execute(
                "INSERT INTO notable_events "
                "(date_key, rank, event_type, session_id, timeline_event_id, "
                "summary, importance, novelty, confidence) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (date_key, rank, c["type"], c.get("sid"), c.get("teid"),
                 c["summary"], c["imp"], c.get("nov", 0.3), c.get("con", 0.5)),
            )

        db_conn.commit()
        self._cursor["last_notable_date"] = date_key
        if deduped:
            log.info("Notable events: %d extracted for %s",
                     len(deduped[:MAX_NOTABLE_PER_DAY]), date_key)

    # ==================================================================
    # HELPERS
    # ==================================================================
    def _insert_session(self, db_conn, stype: str, dk: str, start: str,
                        end: str, dur: float, state: str, ec: int,
                        eids: list[int], summary: str, meta: dict) -> int | None:
        try:
            cur = db_conn.execute(
                "INSERT INTO timeline_sessions "
                "(session_type, date_key, session_start, session_end, "
                "duration_secs, dominant_state, event_count, source_events, "
                "summary, metadata) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (stype, dk, start, end, dur, state, ec,
                 json.dumps(eids), summary, json.dumps(meta) if meta else None),
            )
            db_conn.commit()
            return cur.lastrowid
        except Exception as exc:
            log.warning("insert_session failed: %s", exc)
            return None

    @staticmethod
    def _parse_ts(s: str) -> datetime:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return datetime.now(timezone.utc)

    @staticmethod
    def _fmt_dur(secs: float) -> str:
        if secs < 60:   return f"{secs:.0f}s"
        if secs < 3600: return f"{secs/60:.1f}m"
        return f"{secs/3600:.1f}h"

    @staticmethod
    def _fmt_session(label: str, s: datetime, e: datetime, d: float) -> str:
        return f"{label} {s.strftime('%H:%M')}-{e.strftime('%H:%M')} ({TimelineCompressor._fmt_dur(d)})"

    @staticmethod
    def _split_session(sess: dict, max_h: float = MAX_FOCUS_SESSION_H) -> list[dict]:
        """Split session at midnight + max_duration boundaries."""
        start = sess["s_dt"]; end = sess["e_dt"]
        if (end - start).total_seconds() <= max_h * 3600 and start.date() == end.date():
            return [sess]
        chunks = []; cursor = start
        while cursor < end:
            midnight = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0)
            ceiling = cursor + timedelta(hours=max_h)
            chunk_end = min(midnight, ceiling, end)
            if chunk_end > cursor:
                chunks.append({"s_dt": cursor, "e_dt": chunk_end,
                              "st": sess["st"], "eids": sess["eids"]})
            cursor = chunk_end
        return chunks or [sess]

    @staticmethod
    def _similar(a: str, b: str) -> bool:
        wa = set(a.lower().split()); wb = set(b.lower().split())
        if not wa or not wb: return False
        return len(wa & wb) / min(len(wa), len(wb)) >= 0.6
