#!/usr/bin/env python3
"""Backfill script — runs data_ingestion against all historical files,
then triggers a correlation scan.

Run once after setting up the ingestion pipeline.
"""

import sys, os
sys.path.insert(0, "~/.hermes/helios-v5/helios")

from datetime import datetime, timezone, timedelta
from pathlib import Path
import json, sqlite3

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
HEALTH_DIR = Path.home() / ".hermes" / "health_data"
DB_PATH = str(DATA_DIR / "helios_brain.db")

# =====================================================================
# Backfill idle data → focus
# =====================================================================
def backfill_idle():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    idle_files = sorted(DATA_DIR.glob("idle_*.jsonl"))
    total = 0

    for f in idle_files:
        for line in f.read_text().strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            afk_secs = entry.get("afk_seconds", entry.get("idle_seconds", 0))
            ts_val = entry.get("timestamp", entry.get("ts", ""))
            if not ts_val or afk_secs <= 0:
                continue

            try:
                conn.execute(
                    "INSERT OR IGNORE INTO focus (ts, state, source, duration_secs) VALUES (?, 'screen_time', 'idle_collector', ?)",
                    (ts_val, afk_secs),
                )
                total += 1
            except sqlite3.Error:
                pass

    conn.commit()
    conn.close()
    return total


# =====================================================================
# Backfill health → metric_snapshots
# =====================================================================
def backfill_health():
    conn = sqlite3.connect(DB_PATH)
    total = 0

    def _extract_date_from_ts(ts_str):
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S %z",
        ]:
            try:
                return datetime.strptime(ts_str.replace("Z", "+00:00"), fmt).strftime("%Y-%m-%d")
            except:
                continue
        return None

    # Sleep
    sleep_file = HEALTH_DIR / "sleep.json"
    if sleep_file.exists():
        sleep_data = json.loads(sleep_file.read_text())
        for date_key, day_data in sleep_data.items():
            entries = day_data.get("entries", [])
            sleep_count = sum(1 for e in entries if e.get("metric") == "sleep_analysis")
            if sleep_count >= 4:
                hours = min(float(sleep_count), 12)
                conn.execute(
                    "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES ('sleep.hours', ?, ?, 'health_backfill')",
                    (hours, date_key),
                )
                total += 1

    # Activity
    activity_file = HEALTH_DIR / "activity.json"
    if activity_file.exists():
        activity_data = json.loads(activity_file.read_text())
        for date_key, day_data in activity_data.items():
            entries = day_data.get("entries", [])
            exercise = sum(
                e.get("qty", 0)
                for e in entries
                if e.get("metric") == "apple_exercise_time"
            )
            if exercise > 0:
                conn.execute(
                    "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES ('activity.minutes_daily', ?, ?, 'health_backfill')",
                    (exercise, date_key),
                )
                total += 1

    conn.commit()
    conn.close()
    return total


# =====================================================================
# Backfill protein → metric_snapshots
# =====================================================================
def backfill_protein():
    protein_file = DATA_DIR / "protein_log.json"
    if not protein_file.exists():
        return 0

    conn = sqlite3.connect(DB_PATH)
    total = 0
    protein_data = json.loads(protein_file.read_text())

    for date_key, values in protein_data.items():
        grams = sum(values)
        conn.execute(
            "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES ('protein.grams_daily', ?, ?, 'protein_backfill')",
            (grams, date_key),
        )
        total += 1

    conn.commit()
    conn.close()
    return total


# =====================================================================
# Backfill location
# =====================================================================
def backfill_location():
    loc_file = DATA_DIR / "icloud_location_sync.json"
    if not loc_file.exists():
        return 0

    loc_data = json.loads(loc_file.read_text())
    if not loc_data.get("lat"):
        return 0

    conn = sqlite3.connect(DB_PATH)
    ts = loc_data.get("ts", "")
    date_str = ts[:10] if ts else datetime.now(timezone.utc).strftime("%Y-%m-%d")

    conn.execute(
        "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES ('location.gps_valid', 1, ?, 'icloud_backfill')",
        (date_str,),
    )
    conn.commit()
    conn.close()
    return 1


# =====================================================================
# Main
# =====================================================================
if __name__ == "__main__":
    print("Backfilling idle data...")
    n = backfill_idle()
    print(f"  → {n} idle/screen_time rows")

    print("Backfilling health data...")
    n = backfill_health()
    print(f"  → {n} health snapshots")

    print("Backfilling protein data...")
    n = backfill_protein()
    print(f"  → {n} protein snapshots")

    print("Backfilling location...")
    n = backfill_location()
    print(f"  → {n} location snapshots")

    # Summarize
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    focus_count = conn.execute("SELECT COUNT(*) as c FROM focus").fetchone()["c"]
    snap_count = conn.execute("SELECT COUNT(*) as c FROM metric_snapshots").fetchone()["c"]
    mood_count = conn.execute("SELECT COUNT(*) as c FROM mood").fetchone()["c"]
    conn.close()

    print(f"\n=== After backfill ===")
    print(f"  focus rows:      {focus_count}")
    print(f"  metric_snapshots: {snap_count}")
    print(f"  mood entries:     {mood_count}")
    print(f"  (mood = 0 until you log moods via check-ins)")
