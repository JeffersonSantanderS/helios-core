"""Helios v6 — Data Ingestion Module.

Runs every tick. Reads raw collector JSON/JSONL files and populates the
SQLite tables (focus, mood, metric_snapshots) that the correlator needs
for pattern detection.

Data sources → target tables:
    idle_*.jsonl       → focus (state='idle', 'screen_time')
    tracked_apps.jsonl → focus (state='gaming', 'working')
    Home Assistant     → metric_snapshots (primary health source, hae.* entities)
    sleep.json         → metric_snapshots (fallback health source)
    activity.json      → metric_snapshots (fallback health source)
    protein_log.json   → metric_snapshots (protein.grams_daily)

Health data priority: Home Assistant first, JSON files as fallback.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("helios.data_ingestion")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
HEALTH_DIR = Path.home() / ".hermes" / "health_data"

# Thresholds
GAMING_DETECTION_APPS = {
    "steam", "steamwebhelper", "steam.exe", "steamwebhelper.exe",
    "cs2", "cs2.exe", "csgo", "csgo.exe",
    "minecraft", "javaw", "javaw.exe",
    "valorant", "valorant.exe",
    "league of legends", "leagueclient", "leagueclient.exe",
    "call of duty", "cod.exe",
    "fortnite", "fortniteclient",
    "destiny2", "destiny2.exe",
    "overwatch", "overwatch.exe",
    "apex legends", "r5apex.exe",
    "baldur's gate 3", "bg3",
    "elden ring", "eldenring.exe",
}

WORK_APPS = {
    "code", "code.exe", "cursor", "cursor.exe",
    "terminal", "windows terminal", "windowsterminal.exe",
    "chrome", "chrome.exe", "firefox", "firefox.exe",
    "obsidian", "obsidian.exe",
    "notion", "notion.exe",
    "slack", "slack.exe",
    "discord", "discord.exe",
}

GMAIL_SIGNAL_CATEGORIES = {
    "bill", "receipt", "subscription", "renewal", "delivery", "appointment",
    "reservation", "travel", "account_security", "government", "banking",
    "insurance", "work", "family_plan", "urgent_notice",
}

GMAIL_TIMELINE_EVENT_TYPES = {
    "bill": "email_finance_signal",
    "renewal": "email_finance_signal",
    "subscription": "email_finance_signal",
    "delivery": "email_delivery_signal",
    "appointment": "email_schedule_signal",
    "reservation": "email_schedule_signal",
    "travel": "email_schedule_signal",
    "account_security": "email_security_signal",
    "urgent_notice": "email_urgent_signal",
}

GMAIL_FORBIDDEN_RAW_FIELDS = {
    "body", "raw_body", "subject", "raw_subject", "snippet", "content",
    "raw", "from", "sender", "raw_ref",
}

GMAIL_ALLOWED_FIELDS = {
    "schema_version", "ts", "email_date", "message_id_hash", "thread_id_hash",
    "from_domain", "sender_label", "category", "summary", "action_required",
    "due_date", "amount", "importance", "confidence", "keywords", "body_stored",
}

GMAIL_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
GMAIL_DOMAIN_RE = re.compile(r"^(unknown|[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)$")

GMAIL_SAFE_SUMMARIES = {
    "bill": "Billing email signal detected.",
    "renewal": "Renewal email signal detected.",
    "subscription": "Subscription email signal detected.",
    "delivery": "Delivery email signal detected.",
    "appointment": "Appointment email signal detected.",
    "reservation": "Reservation email signal detected.",
    "travel": "Travel email signal detected.",
    "account_security": "Account security email signal detected.",
    "government": "Government email signal detected.",
    "banking": "Banking email signal detected.",
    "insurance": "Insurance email signal detected.",
    "work": "Work email signal detected.",
    "family_plan": "Family plan email signal detected.",
    "urgent_notice": "Urgent email notice detected.",
    "receipt": "Receipt email signal detected.",
}



def _get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# ============================================================================
# Idle data → focus table
# ============================================================================

# ============================================================================
# Idle data → DISABLED (was poisoning focus table with idle_sec as screen_time)
# ============================================================================

def _ingest_idle(db_path: str, today_str: str) -> int:
    """Parse today's idle JSONL.

    Previously this wrote idle_sec as 'screen_time' duration into the focus table,
    which poisoned the table with inflated numbers (idle_sec accumulates forever
    when the user is away, e.g. overnight = 192,000 seconds reported as "screen time").

    Now: we intentionally do NOT write to the focus table. Screen time is tracked
    correctly by the active_window_tracker via focus_state.json (completed session
    minutes only, with 5-min idle cutoff and 4-hour session cap).

    Returns 0 — no rows written.
    """
    return 0


# ============================================================================
# Active window tracker → gaming / working detection
# ============================================================================

def _ingest_apps(db_path: str, today_str: str) -> int:
    """Parse tracked_apps.jsonl and detect gaming/working sessions.

    Duration fix (2026-06-02): previously wrote duration from JSON entry
    which came from idle_seconds. Now uses FIXED_POLL_INTERVAL_SEC=30
    since tracker polls every 30s.
    """
    app_file = DATA_DIR / "tracked_apps.jsonl"
    if not app_file.exists():
        return 0

    conn = _get_conn(db_path)
    inserted = 0

    try:
        for line in app_file.read_text().strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            app_name = entry.get("app", "")
            ts_val = entry.get("ts", "")
            # Use fixed poll interval — NOT idle_seconds which is cumulative AFK time
            FIXED_POLL_INTERVAL_SEC = 30
            duration = FIXED_POLL_INTERVAL_SEC

            if not app_name or not ts_val:
                continue

            # Gaming detection
            is_gaming = any(g in app_name for g in GAMING_DETECTION_APPS)
            is_working = any(w in app_name for w in WORK_APPS)

            if is_gaming:
                state = "gaming"
            elif is_working:
                state = "working"
            else:
                continue  # Only track meaningful states

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO focus (ts, state, source, duration_secs, context)
                       VALUES (?, ?, 'window_tracker', ?, ?)""",
                    (ts_val, state, duration, json.dumps({"app": app_name})),
                )
                inserted += 1
            except sqlite3.Error:
                pass

        conn.commit()
    except Exception as exc:
        logger.warning("App ingestion failed: %s", exc)
    finally:
        conn.close()

    return inserted


# ============================================================================
# Health data → metric_snapshots (Home Assistant primary, JSON fallback)
# ============================================================================

def _ingest_health_from_ha(db_path: str, cfg: Any) -> dict[str, int]:
    """Pull health data from Home Assistant hae.* sensors.

    Returns dict like {'sleep': 1, 'activity': 1, ...} with count of metrics written.
    Returns empty dict if HA is unreachable or has no data.
    """
    import os
    import subprocess

    ha_cfg = cfg.get("home_assistant") or {}

    base_url = ha_cfg.get("base_url", "") or os.environ.get("HOME_ASSISTANT_URL", "") or os.environ.get("HASS_URL", "")
    prefix = ha_cfg.get("health_prefix", os.environ.get("HA_HEALTH_PREFIX", "hae.healthsync_"))
    timeout = int(ha_cfg.get("health_timeout", 15))
    stale_hours = int(ha_cfg.get("health_stale_hours", 12))

    # Load HA token from .env
    token = os.environ.get("HASS_TOKEN", "")
    if not token:
        try:
            r = subprocess.run(
                ["bash", "-c", "source ~/.hermes/.env 2>/dev/null && printf '%s' \"$HASS_TOKEN\""],
                capture_output=True, text=True, timeout=5,
            )
            token = r.stdout.strip()
        except Exception:
            pass

    if not token:
        logger.warning("HA health ingestion skipped: no HASS_TOKEN available")
        return {}

    from .ha_client import fetch_health_entities, extract_metrics

    entities = fetch_health_entities(base_url, token, prefix=prefix, timeout=timeout)
    if not entities:
        logger.debug("HA health ingestion: no entities returned (HA unreachable or no hae.* sensors)")
        return {}

    extracted = extract_metrics(entities)
    metrics = extracted.get("metrics", {})

    if extracted.get("health_data_stale"):
        logger.warning(
            "HA health data is STALE (last_sync: %s, stale_hours: %d)",
            extracted.get("last_sync", "unknown"), stale_hours,
        )

    if not metrics:
        logger.debug("HA health ingestion: entities found but no values to map")
        return {}

    conn = _get_conn(db_path)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    written = {}

    try:
        for helios_metric, value in metrics.items():
            # Derive a short category key from the metric name
            category = helios_metric.split(".")[0]  # "sleep", "health", "activity"
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO metric_snapshots
                       (metric, value, date_key, source)
                       VALUES (?, ?, ?, 'home_assistant_health')""",
                    (helios_metric, value, today_str),
                )
                written[category] = written.get(category, 0) + 1
            except sqlite3.Error:
                pass

        # Also store sync metadata as a special metric
        last_sync = extracted.get("last_sync", "")
        if last_sync:
            # Use the last_sync timestamp as value=1 marker with the timestamp as context
            conn.execute(
                """INSERT OR REPLACE INTO metric_snapshots
                   (metric, value, date_key, source)
                   VALUES ('health.ha_last_sync_epoch', ?, ?, 'home_assistant_health')""",
                (datetime.fromisoformat(last_sync).timestamp() if last_sync else 0, today_str),
            )

        conn.commit()
        logger.info(
            "HA health ingested: %d metrics across %d categories (%d entities, %d mapped)",
            sum(written.values()), len(written), extracted.get("entity_count", 0), extracted.get("mapped_count", 0),
        )
    except Exception as exc:
        logger.warning("HA health ingestion write failed: %s", exc)
    finally:
        conn.close()

    return written


def _compute_sleep_hours(sleep_data: dict) -> Optional[float]:
    """Extract sleep hours from today's sleep data (JSON fallback only)."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today_str not in sleep_data:
        return None
    entries = sleep_data[today_str].get("entries", [])
    sleep_count = sum(1 for e in entries if e.get("metric") == "sleep_analysis")
    if sleep_count >= 1:
        return float(min(sleep_count, 12))
    return None


def _compute_activity_minutes(activity_data: dict) -> Optional[float]:
    """Extract exercise minutes from today's activity data (JSON fallback only)."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today_str not in activity_data:
        return None
    entries = activity_data[today_str].get("entries", [])
    exercise_entries = [e for e in entries if e.get("metric") == "apple_exercise_time"]
    if exercise_entries:
        return sum(e.get("qty", 0) for e in exercise_entries)
    return None


def _ingest_health_json(db_path: str) -> dict[str, int]:
    """Fallback: read health JSON files and store daily snapshots.

    Only used when Home Assistant is unreachable or returns no data.
    """
    results = {"sleep": 0, "activity": 0}

    sleep_file = HEALTH_DIR / "sleep.json"
    activity_file = HEALTH_DIR / "activity.json"

    conn = _get_conn(db_path)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        # Sleep hours
        if sleep_file.exists():
            try:
                sleep_data = json.loads(sleep_file.read_text())
                hours = _compute_sleep_hours(sleep_data)
                if hours is not None:
                    conn.execute(
                        """INSERT OR REPLACE INTO metric_snapshots
                           (metric, value, date_key, source)
                           VALUES ('sleep.hours', ?, ?, 'health_collector')""",
                        (hours, today_str),
                    )
                    results["sleep"] = 1
            except Exception as exc:
                logger.debug("Sleep JSON ingestion skipped: %s", exc)

        # Activity minutes
        if activity_file.exists():
            try:
                activity_data = json.loads(activity_file.read_text())
                minutes = _compute_activity_minutes(activity_data)
                if minutes is not None and minutes > 0:
                    conn.execute(
                        """INSERT OR REPLACE INTO metric_snapshots
                           (metric, value, date_key, source)
                           VALUES ('activity.minutes_daily', ?, ?, 'health_collector')""",
                        (minutes, today_str),
                    )
                    results["activity"] = 1
            except Exception as exc:
                logger.debug("Activity JSON ingestion skipped: %s", exc)

        conn.commit()
    except Exception as exc:
        logger.warning("Health JSON ingestion failed: %s", exc)
    finally:
        conn.close()

    return results


# ============================================================================
# Protein log → metric_snapshots
# ============================================================================

def _ingest_protein(db_path: str) -> int:
    """Read protein_log.json and store today's total grams."""
    protein_file = DATA_DIR / "protein_log.json"
    if not protein_file.exists():
        return 0

    conn = _get_conn(db_path)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        protein_data = json.loads(protein_file.read_text())
        # protein_log.json is {date: [grams, grams, ...]}
        if today_str in protein_data:
            grams_today = sum(protein_data[today_str])
            conn.execute(
                """INSERT OR REPLACE INTO metric_snapshots
                   (metric, value, date_key, source)
                   VALUES ('protein.grams_daily', ?, ?, 'protein_log')""",
                (grams_today, today_str),
            )
            conn.commit()
            return 1

        # Also try YYYY-MM-DD format keys
        for key in protein_data:
            if today_str in key:
                grams_today = sum(protein_data[key])
                conn.execute(
                    """INSERT OR REPLACE INTO metric_snapshots
                       (metric, value, date_key, source)
                       VALUES ('protein.grams_daily', ?, ?, 'protein_log')""",
                    (grams_today, today_str),
                )
                conn.commit()
                return 1

    except Exception as exc:
        logger.debug("Protein ingestion skipped: %s", exc)
    finally:
        conn.close()

    return 0


# ============================================================================
# Location → metric_snapshots (optional, for future patterns)
# ============================================================================

def _ingest_location(db_path: str) -> int:
    """Store location change indicator from icloud sync."""
    loc_file = DATA_DIR / "icloud_location_sync.json"
    if not loc_file.exists():
        return 0

    conn = _get_conn(db_path)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        loc_data = json.loads(loc_file.read_text())
        if loc_data.get("source") in ("pyicloud", "home_assistant") and loc_data.get("lat"):
            # Store that we had a valid GPS fix today (binary indicator)
            conn.execute(
                """INSERT OR REPLACE INTO metric_snapshots
                   (metric, value, date_key, source)
                   VALUES ('location.gps_valid', 1, ?, 'icloud_sync')""",
                (today_str,),
            )
            conn.commit()
            return 1
    except Exception:
        pass
    finally:
        conn.close()

    return 0


# ============================================================================
# Spotify → metric_snapshots (listening minutes + track count)
# ============================================================================

def _ingest_spotify(db_path: str) -> int:
    """Calculate actual listen time from imperial poll data (logged every 15s).

    The new poller logs every poll with progress_ms. We detect actual listening
    by comparing sequential polls: if progress_ms advances by approximately the
    poll interval (±5s tolerance), you were actually listening. Skips, pauses,
    and inactivity produce gaps or stalled progress that we exclude.

    Returns number of metrics written (2: listen_minutes + tracks_played).
    """
    history_file = DATA_DIR / "spotify_history.jsonl"
    if not history_file.exists():
        return 0

    conn = _get_conn(db_path)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        today_entries: list[dict] = []
        for line in history_file.read_text().strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                entry_date = entry.get("ts", "")[:10]
                if entry_date == today_str:
                    today_entries.append(entry)
            except json.JSONDecodeError:
                continue

        if not today_entries:
            return 0

        # Reconstruct actual listen time from progress_ms deltas
        total_listen_ms = 0
        unique_track_ids: set[str] = set()
        last_progress = None
        last_ts = None
        last_track_id = None

        for entry in today_entries:
            track_id = entry.get("track_id")
            progress = entry.get("progress_ms")
            is_playing = entry.get("is_playing", False)

            if track_id:
                unique_track_ids.add(track_id)

            if not is_playing or progress is None:
                last_progress = None
                last_ts = None
                last_track_id = None
                continue

            if last_progress is not None and track_id == last_track_id:
                delta_ms = progress - last_progress
                # Valid: progress advanced by ~15s (±8s tolerance for API jitter)
                # but not more than 30s (skips ahead, different play session)
                if 7000 <= delta_ms <= 30000:
                    total_listen_ms += delta_ms
                # Small progress (paused/resumed in same 15s window): still counts
                elif 0 < delta_ms < 7000:
                    total_listen_ms += delta_ms

            last_progress = progress
            last_ts = entry.get("ts")
            last_track_id = track_id

        listen_minutes = round(total_listen_ms / 60000, 1)
        tracks_played = len(unique_track_ids)

        if listen_minutes > 0 or tracks_played > 0:
            conn.execute(
                """INSERT OR REPLACE INTO metric_snapshots
                   (metric, value, date_key, source)
                   VALUES ('spotify.listen_minutes_daily', ?, ?, 'spotify_collector')""",
                (listen_minutes, today_str),
            )
            conn.execute(
                """INSERT OR REPLACE INTO metric_snapshots
                   (metric, value, date_key, source)
                   VALUES ('spotify.tracks_daily', ?, ?, 'spotify_collector')""",
                (tracks_played, today_str),
            )
            conn.commit()
            logger.debug(
                "Spotify ingested: %.1f min listened, %d tracks",
                listen_minutes, tracks_played,
            )
            return 2

    except Exception as exc:
        logger.debug("Spotify ingestion skipped: %s", exc)
    finally:
        conn.close()

    return 0


# ============================================================================
# Weather → metric_snapshots (temperature + conditions)
# ============================================================================

def _ingest_weather(db_path: str) -> int:
    """Store weather from Open-Meteo API."""
    import urllib.request
    conn = _get_conn(db_path)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        url = "https://api.open-meteo.com/v1/forecast?latitude=51.16&longitude=-113.96&daily=temperature_2m_max,temperature_2m_min,precipitation_sum&timezone=America%2FEdmonton&forecast_days=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Helios/6.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        daily = data.get("daily", {})
        if daily:
            temp_max = daily.get("temperature_2m_max", [None])[0]
            temp_min = daily.get("temperature_2m_min", [None])[0]
            precip = daily.get("precipitation_sum", [None])[0]

            if temp_max is not None:
                conn.execute(
                    """INSERT OR REPLACE INTO metric_snapshots
                       (metric, value, date_key, source)
                       VALUES ('weather.temp_max', ?, ?, 'open_meteo')""",
                    (temp_max, today_str),
                )
            if temp_min is not None:
                conn.execute(
                    """INSERT OR REPLACE INTO metric_snapshots
                       (metric, value, date_key, source)
                       VALUES ('weather.temp_min', ?, ?, 'open_meteo')""",
                    (temp_min, today_str),
                )
            if precip is not None:
                conn.execute(
                    """INSERT OR REPLACE INTO metric_snapshots
                       (metric, value, date_key, source)
                       VALUES ('weather.precipitation', ?, ?, 'open_meteo')""",
                    (precip, today_str),
                )
            conn.commit()
            # ALSO write weather_state.json for consumers (tick script, Obsidian)
            weather_cache = {
                "source": "open_meteo",
                "ts": datetime.now(timezone.utc).isoformat(),
                "temp_max": temp_max,
                "temp_min": temp_min,
                "precipitation": precip,
            }
            weather_file = DATA_DIR / "weather_state.json"
            weather_file.write_text(json.dumps(weather_cache, indent=2))
            return 1
    except Exception as exc:
        logger.debug("Weather ingestion skipped: %s", exc)
    finally:
        conn.close()

    return 0


# ============================================================================
# Mood → metric_snapshots
# ============================================================================

def _ingest_mood(db_path: str) -> int:
    """Read today's mood score from mood_state.json → metric_snapshots."""
    mood_file = DATA_DIR / "mood_state.json"
    if not mood_file.exists():
        return 0

    conn = _get_conn(db_path)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        mood_data = json.loads(mood_file.read_text())
        for entry in reversed(mood_data.get("history", [])):
            if entry.get("date") == today_str:
                score = entry["score"]
                conn.execute(
                    """INSERT OR REPLACE INTO metric_snapshots
                       (metric, value, date_key, source)
                       VALUES ('mood.score_daily', ?, ?, 'mood_checkin')""",
                    (score, today_str),
                )
                conn.commit()
                return 1
    except Exception as exc:
        logger.debug("Mood ingestion skipped: %s", exc)
    finally:
        conn.close()

    return 0


def _ingest_nutrition(db_path: str) -> int:
    """Parse today's nutrition from nutrition_log.md → metric_snapshots.

    Reads the markdown log file (structured table format) and writes
    calories, protein, carbs, fat, weight, and workout metrics.
    Replaces the old SparkyFitness dependency.
    """
    import re

    nutrition_file = DATA_DIR / "nutrition_log.md"
    if not nutrition_file.exists():
        return 0

    conn = _get_conn(db_path)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        content = nutrition_file.read_text()
        day_header = f"## {today_str}"
        match = re.search(rf"^## {re.escape(today_str)}\s*$", content, re.MULTILINE)
        if not match:
            return 0

        start = match.end()
        next_match = re.search(r"^## \d{4}-\d{2}-\d{2}", content[start:])
        section = content[start:start + next_match.start()] if next_match else content[start:]

        # Parse table rows
        total_cal = 0.0
        total_protein = 0.0
        total_carbs = 0.0
        total_fat = 0.0
        entries = 0

        for line in section.split("\n"):
            line = line.strip()
            if not line.startswith("|"):
                continue
            if "time" in line.lower() and "item" in line.lower():
                continue
            if re.match(r"^\|[-\s|]+\|$", line):
                continue
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 4:
                try:
                    cal = float(parts[2]) if parts[2] else 0
                    protein = float(parts[3]) if len(parts) > 3 and parts[3] else 0
                    carbs = float(parts[4]) if len(parts) > 4 and parts[4] else 0
                    fat = float(parts[5]) if len(parts) > 5 and parts[5] else 0
                    total_cal += cal
                    total_protein += protein
                    total_carbs += carbs
                    total_fat += fat
                    entries += 1
                except (ValueError, IndexError):
                    continue

        # Parse weight
        weight = None
        for line in section.split("\n"):
            line = line.strip()
            if line.startswith("- weight:"):
                try:
                    weight = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass

        if entries > 0:
            metrics = {
                "nutrition.calories_daily": total_cal,
                "nutrition.protein_daily": total_protein,
                "nutrition.carbs_daily": total_carbs,
                "nutrition.fat_daily": total_fat,
            }
            if weight:
                metrics["nutrition.weight"] = weight
            for metric, value in metrics.items():
                conn.execute(
                    """INSERT OR REPLACE INTO metric_snapshots
                       (metric, value, date_key, source)
                       VALUES (?, ?, ?, 'nutrition_log')""",
                    (metric, value, today_str),
                )
            conn.commit()
            return entries
        return 0
    except Exception as exc:
        logger.debug("Nutrition ingestion skipped: %s", exc)
    finally:
        conn.close()

    return 0


# ============================================================================
# Tracked apps → focus table (gaming/working detection from app tracker)
# ============================================================================

def _ingest_tracked_apps(db_path: str, today_str: str) -> int:
    """Parse tracked_apps.jsonl and insert gaming/working sessions into focus.

    Duration fix (2026-06-02): previously wrote idle_seconds as duration_secs.
    idle_seconds is the cumulative time since last input (can be 192,000 overnight).
    Now we use FIXED_POLL_INTERVAL_SEC=30 since tracker polls every 30s.
    Each entry represents one 30-second segment at that window.
    """
    app_file = DATA_DIR / "tracked_apps.jsonl"
    if not app_file.exists():
        return 0

    conn = _get_conn(db_path)
    inserted = 0

    try:
        for line in app_file.read_text().strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts_val = entry.get("ts", "")
            entry_date = ts_val[:10] if ts_val else ""
            if entry_date != today_str:
                continue  # only ingest today's entries

            category = entry.get("category", "unknown")
            title = entry.get("title", "")
            process = entry.get("process", "")
            idle_secs = entry.get("idle_seconds", 0)

            # Map categories to focus states
            if category == "gaming":
                state = "gaming"
            elif category in ("development", "productivity"):
                state = "working"
            else:
                continue  # only ingest meaningful states

            # Fix: use fixed poll interval (30s) as duration, NOT idle_secs
            # idle_secs is cumulative since last input (poisoned overnight)
            FIXED_POLL_INTERVAL_SEC = 30

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO focus (ts, state, source, duration_secs, context)
                       VALUES (?, ?, 'app_tracker', ?, ?)""",
                    (ts_val, state, FIXED_POLL_INTERVAL_SEC, json.dumps({"app": title, "process": process, "category": category})),
                )
                inserted += 1
            except sqlite3.Error:
                pass

        conn.commit()
    except Exception as exc:
        logger.debug("Tracked apps ingestion skipped: %s", exc)
    finally:
        conn.close()

    return inserted


# ============================================================================
# Gmail/Himalaya summary signals → metric_snapshots + timeline_events
# ============================================================================

def _gmail_existing_message_hashes(conn: sqlite3.Connection) -> set[str]:
    """Return message_id_hash values already represented in Gmail timeline rows."""
    existing: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT metadata FROM timeline_events WHERE source_module = 'gmail'"
        ).fetchall()
    except sqlite3.Error:
        return existing
    for row in rows:
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["metadata"]
        if not raw:
            continue
        try:
            metadata = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        mid = metadata.get("message_id_hash")
        if isinstance(mid, str) and mid:
            existing.add(mid)
    return existing


def _valid_gmail_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _valid_gmail_due_date(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value).date()
        return True
    except ValueError:
        return False


def _valid_gmail_signal(record: dict[str, Any]) -> bool:
    """Validate privacy and schema constraints for a Gmail signal record."""
    if set(record.keys()) - GMAIL_ALLOWED_FIELDS:
        return False
    if record.get("schema_version") != 1:
        return False
    if record.get("body_stored") is not False:
        return False
    if GMAIL_FORBIDDEN_RAW_FIELDS.intersection(record.keys()):
        return False
    try:
        confidence = float(record.get("confidence") or 0)
        importance = float(record.get("importance") or 0)
    except (TypeError, ValueError):
        return False
    if confidence < 0.5 or not 0 <= importance <= 1:
        return False
    category = record.get("category")
    if category not in GMAIL_SIGNAL_CATEGORIES:
        return False
    mid = record.get("message_id_hash")
    if not isinstance(mid, str) or not GMAIL_HASH_RE.match(mid):
        return False
    thread_id = record.get("thread_id_hash")
    if not isinstance(thread_id, str) or not GMAIL_HASH_RE.match(thread_id):
        return False
    domain = record.get("from_domain")
    if not isinstance(domain, str) or "@" in domain or not GMAIL_DOMAIN_RE.match(domain):
        return False
    if not _valid_gmail_datetime(record.get("ts")):
        return False
    if not _valid_gmail_datetime(record.get("email_date")):
        return False
    if not _valid_gmail_due_date(record.get("due_date")):
        return False
    keywords = record.get("keywords")
    if keywords is not None and not (isinstance(keywords, list) and all(isinstance(k, str) for k in keywords)):
        return False
    amount = record.get("amount")
    if amount is not None and not isinstance(amount, (int, float)):
        return False
    return True


def _gmail_metric_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "gmail.signals_daily": len(records),
        "gmail.action_required_daily": sum(1 for r in records if bool(r.get("action_required"))),
        "gmail.bills_daily": sum(1 for r in records if r.get("category") == "bill"),
        "gmail.deliveries_daily": sum(1 for r in records if r.get("category") == "delivery"),
        "gmail.appointments_daily": sum(1 for r in records if r.get("category") in {"appointment", "reservation", "travel"}),
        "gmail.security_daily": sum(1 for r in records if r.get("category") == "account_security"),
    }


def _ingest_gmail_signals(db_path: str, today_str: str) -> int:
    """Ingest summary-only Gmail life signals written by the Himalaya collector.

    The collector owns Gmail access. Helios only reads normalized JSONL records
    from DATA_DIR and stores counters plus high-value timeline events. Raw email
    body fields are never read into metadata and never persisted.
    """
    signals_file = DATA_DIR / f"gmail_signals_{today_str}.jsonl"
    if not signals_file.exists():
        return 0

    valid_by_hash: dict[str, dict[str, Any]] = {}
    try:
        for line in signals_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or not _valid_gmail_signal(record):
                continue
            mid = record["message_id_hash"]
            valid_by_hash.setdefault(mid, record)
    except Exception as exc:
        logger.warning("Gmail signal ingestion read failed: %s", exc)
        return 0

    if not valid_by_hash:
        return 0

    conn = _get_conn(db_path)
    inserted_events = 0
    try:
        all_valid = list(valid_by_hash.values())
        for metric, value in _gmail_metric_counts(all_valid).items():
            conn.execute(
                """INSERT OR REPLACE INTO metric_snapshots
                   (metric, value, date_key, source)
                   VALUES (?, ?, ?, 'gmail_himalaya')""",
                (metric, float(value), today_str),
            )

        existing = _gmail_existing_message_hashes(conn)
        for record in all_valid:
            mid = record["message_id_hash"]
            if mid in existing:
                continue
            category = str(record.get("category"))
            event_type = GMAIL_TIMELINE_EVENT_TYPES.get(category)
            if not event_type:
                continue
            metadata = {
                "category": category,
                "from_domain": str(record.get("from_domain") or "unknown")[:120],
                "message_id_hash": mid,
                "action_required": bool(record.get("action_required")),
                "due_date": record.get("due_date"),
                "amount": record.get("amount"),
                "confidence": float(record.get("confidence") or 0),
                "body_stored": False,
            }
            ts_val = str(record.get("email_date") or record.get("ts") or datetime.now(timezone.utc).isoformat())
            summary = GMAIL_SAFE_SUMMARIES.get(category, "Email life signal detected.")
            try:
                importance_raw = record.get("importance")
                importance = float(importance_raw) if importance_raw is not None else 0.5
            except (TypeError, ValueError):
                importance = 0.5
            conn.execute(
                """INSERT INTO timeline_events
                   (ts, event_type, source_module, importance, summary, metadata, date_key)
                   VALUES (?, ?, 'gmail', ?, ?, ?, ?)""",
                (
                    ts_val,
                    event_type,
                    max(0.0, min(1.0, importance)),
                    summary,
                    json.dumps(metadata, sort_keys=True),
                    today_str,
                ),
            )
            existing.add(mid)
            inserted_events += 1

        conn.commit()
        if inserted_events:
            logger.info("Gmail signals ingested: %d timeline events, %d metrics", inserted_events, 6)
    except Exception as exc:
        logger.warning("Gmail signal ingestion failed: %s", exc)
        conn.rollback()
        return 0
    finally:
        conn.close()

    return inserted_events


# ============================================================================
# Main ingestion entry point — called every tick
# ============================================================================

def run_ingestion(db_path: str, cfg: Any = None) -> dict[str, int]:
    """Run all ingestion steps. Returns counts of rows inserted per source.

    Called by the engine during each tick. Should be idempotent (INSERT OR IGNORE).

    Health data priority: Home Assistant (hae.* sensors) first.
    Falls back to local JSON files only if HA is unreachable or returns no data.
    """
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counts = {}

    # Phase 1: Raw data → focus table
    counts["idle"] = _ingest_idle(db_path, today_str)
    counts["apps"] = _ingest_apps(db_path, today_str)
    counts["tracked_apps"] = _ingest_tracked_apps(db_path, today_str)

    # Phase 2: Health → metric_snapshots (HA-first, JSON fallback)
    if cfg is not None:
        ha_counts = _ingest_health_from_ha(db_path, cfg)
        if ha_counts:
            counts["ha_health"] = sum(ha_counts.values())
            # Still try JSON for backward compat on unmapped metrics (sleep.hours, activity.minutes_daily)
            # But only if HA didn't already write equivalents
            json_counts = _ingest_health_json(db_path)
            # Don't double-count — HA is the source of truth
        else:
            logger.info("HA health unavailable, falling back to JSON files")
            json_counts = _ingest_health_json(db_path)
            counts.update(json_counts)
    else:
        json_counts = _ingest_health_json(db_path)
        counts.update(json_counts)

    # Phase 3: Protein → metric_snapshots
    counts["protein"] = _ingest_protein(db_path)

    # Phase 4: Location → metric_snapshots
    counts["location"] = _ingest_location(db_path)

    # Phase 5: Spotify → metric_snapshots
    counts["spotify"] = _ingest_spotify(db_path)

    # Phase 6: Weather → metric_snapshots
    counts["weather"] = _ingest_weather(db_path)

    # Phase 7: Mood → metric_snapshots
    counts["mood"] = _ingest_mood(db_path)

    # Phase 8: Nutrition → metric_snapshots (replaces SparkyFitness)
    counts["nutrition"] = _ingest_nutrition(db_path)

    # Phase 9: Gmail/Himalaya summary signals → metric_snapshots + timeline_events
    counts["gmail"] = _ingest_gmail_signals(db_path, today_str)

    total = sum(counts.values())
    if total > 0:
        logger.info(
            "Ingestion tick: %d rows (%s)",
            total,
            ", ".join(f"{k}={v}" for k, v in counts.items() if v > 0),
        )

    return counts
