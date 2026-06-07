"""Helios v6 — Daily Intelligence Loop.

Replaces the old BriefingModule context-table reads with
live v6 data sources: metric_snapshots, focus, mood, correlator, weather module.

Four components:
  1. Morning Briefing  — sleep, weather, calendar, health trends, patterns
  2. Evening Wrap      — today's focus breakdown, mood, activity, Spotify, wind-down
  3. Daytime Interrupt — focus-aware alert gating (defer non-urgent during work)
  4. External Export   — metric_snapshots + focus + correlations for downstream sync
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .preference_engine import PreferenceEngine
    from .module_health import ModuleHealthTracker

logger = logging.getLogger("helios.intelligence")

# ── Thresholds ─────────────────────────────────────────────────────────────
SLEEP_LOW = 5.0        # hours — warn below this
SLEEP_GOOD = 7.0       # hours — green zone
ACTIVITY_LOW = 20      # minutes — couch-potato warning
ACTIVITY_GOOD = 60     # minutes — healthy
MOOD_LOW = 4           # concern threshold
FOCUS_WORK_MIN = 3600  # 1hr minimum productive focus to mention
GAMING_MARATHON = 7200 # 2hr — mention in evening wrap
SPOTIFY_HEAVY = 120    # minutes — "late night listening?"

# ── Embed colors ────────────────────────────────────────────────────────────
COLOR_MORNING  = 0x3498DB  # blue
COLOR_EVENING  = 0x2ECC71  # green
COLOR_INTERRUPT = 0xE74C3C # red for urgent-only
COLOR_PATTERNS = 0x9B59B6  # purple

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today_utc() -> str:
    return _now_utc().strftime("%Y-%m-%d")


def _progress_bar(current: float, target: float, width: int = 10) -> str:
    if target <= 0:
        return "░" * width
    ratio = min(max(current / target, 0), 1.0)
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)


def _format_minutes(minutes: float) -> str:
    """Human-readable duration from minutes."""
    if minutes < 1:
        return "<1m"
    hrs = int(minutes // 60)
    mins = int(minutes % 60)
    if hrs == 0:
        return f"{mins}m"
    if mins == 0:
        return f"{hrs}h"
    return f"{hrs}h{mins}m"


def _format_hours(hours: float) -> str:
    if hours < 0.05:
        return "0h"
    h = int(hours)
    m = int((hours - h) * 60)
    if h == 0:
        return f"{m}m"
    if m == 0:
        return f"{h}h"
    return f"{h}h{m}m"


# ═══════════════════════════════════════════════════════════════════════════
# 1. Morning Briefing
# ═══════════════════════════════════════════════════════════════════════════

def generate_morning(db_path: str, module_context: dict[str, Any],
                     prefs: Optional["PreferenceEngine"] = None,
                     health: Optional["ModuleHealthTracker"] = None) -> dict[str, Any]:
    """Morning briefing from live v6 data sources.

    Reads:
      - metric_snapshots: sleep.hours (last night), activity yesterday
      - weather module: temp_c, condition, forecast
      - calendar module: today's events
      - mood: yesterday's score
      - correlator: top patterns
      - spotify: yesterday's listening

    Returns Discord embed dict + raw content.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    today = _today_utc()
    yesterday = (_now_utc() - timedelta(days=1)).strftime("%Y-%m-%d")

    content: dict[str, Any] = {"date": today}
    fields: list[dict[str, Any]] = []

    # ── Sleep ──────────────────────────────────────────────────────────
    try:
        row = conn.execute(
            "SELECT value FROM metric_snapshots WHERE metric='sleep.hours' AND date_key=?",
            (yesterday,)
        ).fetchone()
        sleep_hours = row["value"] if row else None

        if sleep_hours is not None:
            low_threshold = prefs.get("low_sleep_alert_h", SLEEP_LOW) if prefs else SLEEP_LOW
            quality = "😴 Low" if sleep_hours < low_threshold else (
                "⚠️ Short" if sleep_hours < SLEEP_GOOD else "✅ Good"
            )
            fields.append({
                "_section": "sleep",
                "name": f"💤 Sleep — {sleep_hours:.1f}h",
                "value": f"{quality}{' — aim for 7h tonight' if sleep_hours < SLEEP_GOOD else ''}",
                "inline": True,
            })
            content["sleep"] = sleep_hours
    except Exception as exc:
        logger.debug("Sleep query: %s", exc)

    # ── Weather ────────────────────────────────────────────────────────
    weather = module_context.get("weather", {})
    if weather and weather.get("temp_c") is not None:
        temp = weather["temp_c"]
        cond = weather.get("condition", "—")
        hi = weather.get("forecast_high", "?")
        lo = weather.get("forecast_low", "?")
        fields.append({
            "_section": "weather",
            "name": f"🌤️ {temp}°C — {cond}",
            "value": f"High {hi}° · Low {lo}°",
            "inline": True,
        })
        content["weather"] = {"temp_c": temp, "condition": cond, "high": hi, "low": lo}

    # ── Calendar ───────────────────────────────────────────────────────
    cal = module_context.get("calendar", {})
    if cal:
        event_count = cal.get("today_count", cal.get("event_count", 0))
        if event_count:
            fields.append({
                "_section": "calendar",
                "name": f"📅 {event_count} event(s) today",
                "value": cal.get("next_title", "Check your calendar"),
                "inline": False,
            })
            content["calendar"] = {"events": event_count}
        else:
            fields.append({
                "_section": "calendar",
                "name": "📅 Clear schedule",
                "value": "No events — protect this time for deep work 🌿",
                "inline": False,
            })

    # ── Activity (yesterday) ───────────────────────────────────────────
    try:
        row = conn.execute(
            "SELECT value FROM metric_snapshots WHERE metric='activity.minutes_daily' AND date_key=?",
            (yesterday,)
        ).fetchone()
        activity_min = row["value"] if row else None
        if activity_min is not None:
            act_low = prefs.get("activity_low_minutes", ACTIVITY_LOW) if prefs else ACTIVITY_LOW
            status = "🛋️ Couch day" if activity_min < act_low else (
                "🚶 Light" if activity_min < ACTIVITY_GOOD else "🏃 Active"
            )
            fields.append({
                "_section": "activity",
                "name": "📊 Yesterday's Activity",
                "value": f"{status} — {_format_minutes(activity_min)}",
                "inline": True,
            })
            content["activity_yesterday"] = activity_min
    except Exception as exc:
        logger.debug("Activity query: %s", exc)

    # ── Mood ───────────────────────────────────────────────────────────
    try:
        row = conn.execute(
            "SELECT value FROM metric_snapshots WHERE metric='mood.score_daily' "
            "AND date_key=? ORDER BY date_key DESC LIMIT 1",
            (yesterday,)
        ).fetchone()
        mood_score = row["value"] if row else None
        if mood_score is not None:
            emoji = "😊" if mood_score >= 7 else ("😐" if mood_score >= 4 else "😔")
            fields.append({
                "_section": "mood",
                "name": f"{emoji} Yesterday's Mood",
                "value": f"{mood_score}/10",
                "inline": True,
            })
            content["mood_yesterday"] = mood_score
    except Exception as exc:
        logger.debug("Mood query: %s", exc)

    # ── Nutrition (yesterday + today so far) ────────────────────────────
    try:
        # Today's nutrition
        today_nutrition = module_context.get("nutrition", {})
        if today_nutrition and today_nutrition.get("entries_today", 0) > 0:
            cal = today_nutrition.get("calories_today", 0)
            cal_target = today_nutrition.get("calorie_target", 2000)
            cal_pct = today_nutrition.get("calorie_pct", 0)
            protein = today_nutrition.get("protein_today", 0)
            protein_target = today_nutrition.get("protein_target", 160)
            protein_pct = today_nutrition.get("protein_pct", 0)
            bar = _progress_bar(cal, cal_target)

            fields.append({
                "_section": "nutrition",
                "name": f"🍽️ Today's Nutrition — {cal:.0f}/{cal_target} cal",
                "value": f"{bar} {cal_pct:.0f}%\n🥩 Protein: {protein:.0f}/{protein_target}g ({protein_pct:.0f}%)",
                "inline": True,
            })
            content["nutrition"] = today_nutrition
        else:
            # Try yesterday's nutrition
            row = conn.execute(
                "SELECT value FROM metric_snapshots WHERE metric='nutrition.calories_daily' AND date_key=?",
                (yesterday,)
            ).fetchone()
            if row and row["value"]:
                yest_cal = row["value"]
                fields.append({
                    "_section": "nutrition",
                    "name": "🍽️ Yesterday's Calories",
                    "value": f"{yest_cal:.0f} cal",
                    "inline": True,
                })
    except Exception as exc:
        logger.debug("Nutrition query: %s", exc)

    # ── Patterns ───────────────────────────────────────────────────────
    try:
        corr_rows = conn.execute(
            "SELECT metric_a, metric_b, window_days, pearson_r, strength, direction "
            "FROM correlations WHERE strength='strong' AND p_value < 0.01 "
            "ORDER BY ABS(pearson_r) DESC LIMIT 3"
        ).fetchall()

        if corr_rows:
            pattern_lines = []
            for r in corr_rows:
                arrow = "↑" if r["direction"] == "positive" else "↓"
                name_a = r["metric_a"].replace("_", " ").replace(".", " → ")
                name_b = r["metric_b"].replace("_", " ").replace(".", " → ")
                pattern_lines.append(
                    f"{arrow} **{name_a}** linked to **{name_b}** "
                    f"(r={r['pearson_r']:.2f}, {r['window_days']}d)"
                )
            fields.append({
                "_section": "patterns",
                "name": "🔗 Patterns Detected",
                "value": "\n".join(pattern_lines),
                "inline": False,
            })
    except Exception:
        pass

    # Add confidence-weighted insight (Phase 2)
    if health:
        low_confidence = [
            name for name in ("weather", "calendar", "spotify", "mood")
            if health.confidence(name) < 0.5 and health.state(name) != "unknown"
        ]
        if low_confidence:
            fields.append({
                "_section": "_confidence",
                "name": "📉 Low-Confidence Data",
                "value": "Some data sources have low confidence today. Take insights with a grain of salt.",
                "inline": False,
            })

    conn.close()

    # ── Phase 2: Stale data warnings ──────────────────────────────────
    if health:
        from .module_health import freshness_warning
        stale_modules = [
            name for name in ("weather", "calendar", "spotify", "mood")
            if health.is_stale(name)
        ]
        if stale_modules:
            stale_lines = []
            for name in stale_modules:
                msg = freshness_warning(health, name)
                if msg:
                    stale_lines.append(f"⚠️ {msg}")
            if stale_lines:
                fields.insert(0, {
                    "_section": "_stale",
                    "name": "⚠️ Stale Data",
                    "value": "\n".join(stale_lines),
                    "inline": False,
                })

    # ── Apply adaptive briefing section ordering (ChatGPT review blocker 6) ─
    if prefs:
        order = prefs.briefing_order()
        # Map section names to indices in the order list
        section_rank = {s: i for i, s in enumerate(order)}
        # Sort fields: system sections (_stale, _confidence) get negative rank
        # to stay above user-ordered content (Phase 2 blocker 6)
        def _rank(field: dict) -> int:
            section = field.get("_section", "")
            if section == "_stale":
                return -100
            if section == "_confidence":
                return -90
            return section_rank.get(section, len(order) + 1)
        fields.sort(key=_rank)

    embed = {
        "title": f"☀️ Morning Briefing — {today}",
        "description": "Here's your day at a glance.",
        "color": COLOR_MORNING,
        "fields": [{k: v for k, v in f.items() if not k.startswith("_")} for f in fields],
        "footer": {"text": "Helios v6 Daily Intelligence"},
        "timestamp": _now_utc().isoformat(),
    }

    return {"embed": embed, "content": content}


# ═══════════════════════════════════════════════════════════════════════════
# 2. Evening Wrap
# ═══════════════════════════════════════════════════════════════════════════

def generate_evening(db_path: str, module_context: dict[str, Any],
                       prefs: Optional["PreferenceEngine"] = None,
                       health: Optional["ModuleHealthTracker"] = None) -> dict[str, Any]:
    """Evening wrap — today's data at a glance.

    Reads metric_snapshots for today + focus table aggregations.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    today = _today_utc()

    content: dict[str, Any] = {"date": today}
    fields: list[dict[str, Any]] = []

    # ── Today's metrics ────────────────────────────────────────────────
    metrics = {}
    for metric_name in [
        "sleep.hours", "activity.minutes_daily", "mood.score_daily",
        "spotify.listen_minutes_daily", "spotify.tracks_daily",
        "resting_heart_rate.avg_daily",
    ]:
        try:
            row = conn.execute(
                "SELECT value FROM metric_snapshots WHERE metric=? AND date_key=?",
                (metric_name, today)
            ).fetchone()
            if row and row["value"] is not None:
                metrics[metric_name] = row["value"]
        except Exception:
            pass

    # Health snapshot
    health_lines = []
    sleep = metrics.get("sleep.hours")
    activity = metrics.get("activity.minutes_daily")
    mood = metrics.get("mood.score_daily")
    hr = metrics.get("resting_heart_rate.avg_daily")

    if sleep is not None:
        low_threshold = prefs.get("low_sleep_alert_h", SLEEP_LOW) if prefs else SLEEP_LOW
        quality = "⚠️" if sleep < low_threshold else ("✓" if sleep >= SLEEP_GOOD else "")
        health_lines.append(f"💤 Sleep: {sleep:.1f}h {quality}")
    if activity is not None:
        health_lines.append(f"🏃 Activity: {_format_minutes(activity)}")
    if hr is not None:
        health_lines.append(f"💓 RHR: {hr:.0f} bpm")
    if mood is not None:
        emoji = "😊" if mood >= 7 else ("😐" if mood >= 4 else "😔")
        health_lines.append(f"{emoji} Mood: {mood}/10")

    if health_lines:
        fields.append({
            "name": "📊 Today's Health",
            "value": "\n".join(health_lines),
            "inline": False,
        })

    # ── Focus breakdown ────────────────────────────────────────────────
    # Screen time: read from focus_state.json (has cumulative completed sessions)
    # NOT from the focus table which is poisoned by idle.jsonl ingestion
    focus_lines = []
    state_icons = {
        "working": "💻", "gaming": "🎮", "idle": "⏸️",
        "meeting": "📞", "break": "☕", "screen_time": "🖥️",
    }

    # 1) Screen time from focus_state.json (live cumulative)
    try:
        from pathlib import Path
        fs_path = Path.home() / ".hermes" / "helios" / "data" / "focus_state.json"
        if fs_path.exists():
            with open(fs_path) as f:
                fs = json.load(f)
            screen_min = int(fs.get("screen_time_today_minutes", 0))
            if screen_min > 0:
                focus_lines.append(f"🖥️ Screen Time   `{_progress_bar(screen_min, max(screen_min, 1), 8)}` {_format_minutes(screen_min)} 100%")
    except Exception as exc:
        logger.debug("Screen time from focus_state: %s", exc)

    # 2) Working / gaming from tracked_apps.jsonl (real window sessions, not idle poison)
    try:
        from pathlib import Path
        tap_path = Path.home() / ".hermes" / "helios" / "data" / "tracked_apps.jsonl"
        if tap_path.exists():
            from collections import defaultdict
            cat_secs: dict[str, float] = defaultdict(int)
            now_ts = datetime.now(timezone.utc)
            with open(tap_path) as f:
                lines = f.readlines()
            # Process only today's entries
            today_str = today + "T"  # UTC date prefix
            prev_ts: dict[str, float] = {}
            for line in lines:
                entry = json.loads(line)
                ts = entry.get("ts", "")
                if not ts.startswith(today_str):
                    continue
                cat = entry.get("category", "unknown")
                if cat not in ("gaming", "development", "productivity", "browser", "communication", "media"):
                    continue
                # Only count when idle is low (actually active)
                idle_sec = entry.get("idle_seconds", 0)
                if idle_sec > 300:
                    continue  # User was idle, skip
                # Add 30s for each active poll
                cat_secs[cat] += 30

            total = sum(cat_secs.values())
            for cat, secs in sorted(cat_secs.items(), key=lambda x: x[1], reverse=True)[:5]:
                if secs < 60:
                    continue
                state = "gaming" if cat == "gaming" else ("working" if cat in ("development", "productivity") else cat)
                icon = state_icons.get(state, "❓")
                pct = round((secs / max(total, 1)) * 100)
                bar = _progress_bar(int(secs), max(int(total), 1), 8)
                focus_lines.append(f"{icon} {state.title():12s} `{bar}` {_format_minutes(secs/60)} ({pct}%)")
    except Exception as exc:
        logger.debug("Tracked apps focus: %s", exc)

    if focus_lines:
        fields.append({
            "name": "🎯 Focus Breakdown",
            "value": "\n".join(focus_lines[:6]),
            "inline": False,
        })

    # ── Spotify ────────────────────────────────────────────────────────
    spotify_min = metrics.get("spotify.listen_minutes_daily")
    spotify_tracks = metrics.get("spotify.tracks_daily")
    if spotify_min is not None and spotify_min > 0:
        detail = f"{_format_minutes(spotify_min)} listening"
        if spotify_tracks:
            detail += f" · {spotify_tracks} tracks"
        heavy_threshold = prefs.get("spotify_heavy_minutes", SPOTIFY_HEAVY) if prefs else SPOTIFY_HEAVY
        if spotify_min > heavy_threshold:
            detail += "\n🎧 Heavy listening day — late night session?"
        fields.append({
            "name": "🎵 Spotify",
            "value": detail,
            "inline": True,
        })

    # ── Gaming check ───────────────────────────────────────────────────
    try:
        row = conn.execute(
            "SELECT SUM(duration_secs) FROM focus "
            "WHERE state='gaming' AND ts LIKE ? || '%'",
            (today,)
        ).fetchone()
        gaming_secs = (row[0] or 0) if row else 0
        marathon_threshold = prefs.get("gaming_marathon_h", 2.0) * 3600 if prefs else GAMING_MARATHON
        if gaming_secs > marathon_threshold:
            hrs = gaming_secs / 3600
            fields.append({
                "name": "🎮 Gaming Marathon",
                "value": f"{hrs:.1f}h — time for a stretch and water break?",
                "inline": False,
            })
    except Exception:
        pass

    # ── Nutrition summary ──────────────────────────────────────────────
    try:
        nutrition = module_context.get("nutrition", {})
        if nutrition and nutrition.get("entries_today", 0) > 0:
            cal = nutrition.get("calories_today", 0)
            cal_target = nutrition.get("calorie_target", 2000)
            protein = nutrition.get("protein_today", 0)
            protein_target = nutrition.get("protein_target", 160)
            cal_pct = nutrition.get("calorie_pct", 0)
            bar = _progress_bar(cal, cal_target)
            nutrition_lines = [
                f"{bar} {cal:.0f}/{cal_target} cal ({cal_pct:.0f}%)",
                f"🥩 {protein:.0f}/{protein_target}g protein",
            ]
            weight = nutrition.get("weight")
            if weight:
                nutrition_lines.append(f"⚖️ Weight: {weight} lbs")
            workout = nutrition.get("workout")
            if workout:
                nutrition_lines.append(f"🏋️ {workout}")
            fields.append({
                "name": "🍽️ Nutrition",
                "value": "\n".join(nutrition_lines),
                "inline": False,
            })
        else:
            # Try from metric_snapshots
            try:
                row = conn.execute(
                    "SELECT value FROM metric_snapshots WHERE metric='nutrition.calories_daily' AND date_key=?",
                    (today,)
                ).fetchone()
                if row and row["value"]:
                    fields.append({
                        "name": "🍽️ Nutrition",
                        "value": f"{row['value']:.0f} cal today",
                        "inline": True,
                    })
            except Exception:
                pass
    except Exception as exc:
        logger.debug("Evening nutrition: %s", exc)

    # ── Wind-down ──────────────────────────────────────────────────────
    if sleep is not None and sleep < SLEEP_GOOD:
        wind_down = f"🌙 You averaged {sleep:.1f}h last night. Early bedtime tonight?"
    elif mood is not None and mood <= MOOD_LOW:
        wind_down = "💙 Rough day — be kind to yourself tonight."
    elif activity is not None and activity < (prefs.get("activity_low_minutes", ACTIVITY_LOW) if prefs else ACTIVITY_LOW):
        wind_down = "🛋️ Low activity day. A quick stretch before bed?"
    else:
        wind_down = "Good day! Time to recharge for tomorrow."

    fields.append({
        "name": "🌙 Wind-Down",
        "value": wind_down,
        "inline": False,
    })

    conn.close()

    embed = {
        "title": f"🌙 Evening Wrap — {today}",
        "description": "Here's how your day went.",
        "color": COLOR_EVENING,
        "fields": fields,
        "footer": {"text": "Helios v6 Daily Intelligence"},
        "timestamp": _now_utc().isoformat(),
    }

    return {"embed": embed, "content": content}


# ═══════════════════════════════════════════════════════════════════════════
# 3. Daytime Interrupt Filter
# ═══════════════════════════════════════════════════════════════════════════

class InterruptFilter:
    """Decides whether an alert should push based on current focus state.

    Rules:
      - priority >= critical: ALWAYS push regardless of state
      - priority 1 during 'working'/'meeting': defer to evening if defer enabled
      - priority 2 during 'working': push if idle > 5 min (break window)
      - any priority during 'idle'/'gaming'/'break': push
      - priority 2 during 'gaming': push with gaming-aware message
      - respects quiet_hours: suppresses priority 1
    """

    CRITICAL_PRIORITY = 3
    DEFER_PRIORITY = 1
    WORK_STATES = {"working", "meeting"}

    @staticmethod
    def get_focus_state(db_path: str) -> tuple[str, float]:
        """Return (state, idle_seconds) from most recent focus entry."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT state, duration_secs FROM focus ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if row:
                return row["state"] or "unknown", 0.0
        except Exception:
            pass
        finally:
            conn.close()
        return "unknown", 0.0

    @staticmethod
    def should_push(db_path: str, alert: dict,
                    prefs: Optional["PreferenceEngine"] = None,
                    health: Optional["ModuleHealthTracker"] = None) -> tuple[bool, str]:
        """Return (should_push, reason)."""
        priority = alert.get("priority", 1)

        # Critical always goes through
        if priority >= InterruptFilter.CRITICAL_PRIORITY:
            return True, "critical"

        # Check quiet hours from prefs
        if prefs and priority <= InterruptFilter.DEFER_PRIORITY:
            if prefs.is_quiet_hours():
                return False, "deferred: quiet_hours"

        # Check if defer during work state is enabled
        if prefs and not prefs.get("interrupt_defer_work", True):
            return True, "work-defer disabled"

        # Check module health: suppress priority 1 alerts from degraded/failed/anomalous
        # modules, UNLESS the alert itself is a health alert (blocker 7).
        source_module = alert.get("source", "")
        if health and source_module and priority <= InterruptFilter.DEFER_PRIORITY:
            if health.state(source_module) in ("degraded", "failed", "anomalous"):
                # Health alerts always get through — don't suppress the symptoms
                from .module_health import is_health_alert
                if not is_health_alert(alert):
                    return False, f"deferred: module {source_module} state={health.state(source_module)}"

        state, _ = InterruptFilter.get_focus_state(db_path)

        # Gaming-aware check BEFORE the generic non-work return.
        # Gaming is not in WORK_STATES, so the gaming_aware branch
        # was previously unreachable. (ChatGPT review blocker 7)
        if (state == "gaming" and prefs
                and prefs.get("interrupt_gaming_aware", True)
                and priority >= 2):
            return True, f"state={state},pri={priority},gaming_aware"

        # Non-working states — push freely
        if state not in InterruptFilter.WORK_STATES:
            return True, f"state={state}"

        # Working/meeting state — only priority 2+ gets through
        if priority >= 2:
            return True, f"state={state},pri={priority}"

        return False, f"deferred: state={state},pri={priority}"


# ═══════════════════════════════════════════════════════════════════════════
# 4. External Export
# ═══════════════════════════════════════════════════════════════════════════

def export_for_downstream(db_path: str) -> dict[str, Any]:
    """Export live v6 data as JSON for downstream system sync.

    Exports:
      - metric_snapshots: last 7 days, grouped by metric
      - focus: today's breakdown by state
      - correlations: all strong patterns
      - mood: last 7 days
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    today = _today_utc()
    week_ago = (_now_utc() - timedelta(days=7)).strftime("%Y-%m-%d")

    result: dict[str, Any] = {
        "export_ts": _now_utc().isoformat(),
        "date_range": {"from": week_ago, "to": today},
        "metric_snapshots": [],
        "focus_today": [],
        "correlations": [],
        "mood_7d": [],
    }

    try:
        # Metrics
        rows = conn.execute(
            "SELECT date_key, metric, value, source FROM metric_snapshots "
            "WHERE date_key >= ? ORDER BY date_key, metric",
            (week_ago,)
        ).fetchall()
        result["metric_snapshots"] = [
            {"date": r["date_key"], "metric": r["metric"],
             "value": r["value"], "source": r["source"]}
            for r in rows
        ]

        # Focus today
        rows = conn.execute(
            "SELECT state, COUNT(*) as cnt, SUM(duration_secs) as total_secs "
            "FROM focus WHERE ts LIKE ? || '%' GROUP BY state",
            (today,)
        ).fetchall()
        result["focus_today"] = [
            {"state": r["state"], "occurrences": r["cnt"],
             "total_seconds": r["total_secs"] or 0}
            for r in rows
        ]

        # Correlations
        rows = conn.execute(
            "SELECT metric_a, metric_b, window_days, pearson_r, strength, "
            "direction, p_value FROM correlations WHERE strength='strong' "
            "ORDER BY ABS(pearson_r) DESC LIMIT 10"
        ).fetchall()
        result["correlations"] = [dict(r) for r in rows]

        # Mood
        rows = conn.execute(
            "SELECT date_key, value FROM metric_snapshots "
            "WHERE metric='mood.score_daily' AND date_key >= ? ORDER BY date_key",
            (week_ago,)
        ).fetchall()
        result["mood_7d"] = [{"date": r["date_key"], "score": r["value"]} for r in rows]

        result["row_count"] = len(result["metric_snapshots"])

    except Exception as exc:
        result["error"] = str(exc)
    finally:
        conn.close()

    return result
