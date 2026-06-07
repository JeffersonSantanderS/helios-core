#!/usr/bin/env python3
"""Backfill script v2 — handles real data field names and corrupted JSON files."""

import sys, os, json, sqlite3
from pathlib import Path

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
HEALTH_DIR = Path.home() / ".hermes" / "health_data"
DB_PATH = str(DATA_DIR / "helios_brain.db")


def _load_corrupted_json(path: Path) -> dict:
    """Load a JSON file that may have been corrupted by double-append."""
    if not path.exists():
        return {}
    raw = path.read_text()
    # Try full parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try just the first valid JSON object
    brace_count = 0
    obj_start = None
    for i, ch in enumerate(raw):
        if ch == '{':
            if brace_count == 0:
                obj_start = i
            brace_count += 1
        elif ch == '}':
            brace_count -= 1
            if brace_count == 0 and obj_start is not None:
                valid_part = raw[obj_start:i+1]
                try:
                    return json.loads(valid_part)
                except json.JSONDecodeError:
                    pass
                obj_start = None
    return {}


# =====================================================================
# Backfill idle: compute active screen time from idle_sec deltas
# =====================================================================
def backfill_idle():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    total = 0

    idle_files = sorted(DATA_DIR.glob("idle_*.jsonl"))
    for f in idle_files:
        lines = [l.strip() for l in f.read_text().split("\n") if l.strip()]
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not entries:
            continue
        # Compute screen time: delta idle_sec < 1 means user was active
        prev_idle = entries[0].get("idle_sec", 0)
        for entry in entries[1:]:
            curr_idle = entry.get("idle_sec", 0)
            ts = entry.get("ts", "")
            delta = curr_idle - prev_idle
            # If idle_sec barely changed (delta ≈ 0), user was active = screen time
            if delta <= 1:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO focus (ts, state, source, duration_secs) VALUES (?, 'screen_time', 'idle_backfill', 60)",
                        (ts,),
                    )
                    total += 1
                except sqlite3.Error:
                    pass
            prev_idle = curr_idle

    conn.commit()
    conn.close()
    return total


# =====================================================================
# Backfill health
# =====================================================================
def backfill_health():
    conn = sqlite3.connect(DB_PATH)
    total = 0

    # Sleep
    sleep_data = _load_corrupted_json(HEALTH_DIR / "sleep.json")
    for date_key, day_data in sleep_data.items():
        if not isinstance(day_data, dict):
            continue
        entries = day_data.get("entries", [])
        sleep_count = sum(1 for e in entries if isinstance(e, dict) and e.get("metric") == "sleep_analysis")
        if sleep_count >= 4:
            hours = min(float(sleep_count), 12)
            conn.execute(
                "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES ('sleep.hours', ?, ?, 'health_backfill')",
                (hours, date_key),
            )
            total += 1

    # Activity
    activity_data = _load_corrupted_json(HEALTH_DIR / "activity.json")
    for date_key, day_data in activity_data.items():
        if not isinstance(day_data, dict):
            continue
        entries = day_data.get("entries", [])
        exercise = sum(
            e.get("qty", 0)
            for e in entries
            if isinstance(e, dict) and e.get("metric") == "apple_exercise_time"
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
# Backfill protein
# =====================================================================
def backfill_protein():
    protein_file = DATA_DIR / "protein_log.json"
    if not protein_file.exists():
        return 0

    conn = sqlite3.connect(DB_PATH)
    total = 0
    protein_data = json.loads(protein_file.read_text())

    for date_key, values in protein_data.items():
        if isinstance(values, list):
            grams = sum(values)
        elif isinstance(values, (int, float)):
            grams = values
        else:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES ('protein.grams_daily', ?, ?, 'protein_backfill')",
            (grams, date_key),
        )
        total += 1

    conn.commit()
    conn.close()
    return total


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
    date_str = ts[:10] if ts else "2026-05-03"

    conn.execute(
        "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES ('location.gps_valid', 1, ?, 'icloud_backfill')",
        (date_str,),
    )
    conn.commit()
    conn.close()
    return 1


# =====================================================================
if __name__ == "__main__":
    print("Backfilling idle → screen_time...")
    n = backfill_idle()
    print(f"  → {n} screen_time rows")

    print("Backfilling health → metric_snapshots...")
    n = backfill_health()
    print(f"  → {n} health snapshots")

    print("Backfilling protein...")
    n = backfill_protein()
    print(f"  → {n} protein snapshots")

    print("Backfilling location...")
    n = backfill_location()
    print(f"  → {n} location snapshots")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    focus_count = conn.execute("SELECT COUNT(*) as c FROM focus").fetchone()["c"]
    snap_count = conn.execute("SELECT COUNT(*) as c FROM metric_snapshots").fetchone()["c"]
    mood_count = conn.execute("SELECT COUNT(*) as c FROM mood").fetchone()["c"]
    conn.close()

    print(f"\n=== After backfill ===")
    print(f"  focus rows:        {focus_count}")
    print(f"  metric_snapshots:  {snap_count}")
    print(f"  mood entries:      {mood_count}")
