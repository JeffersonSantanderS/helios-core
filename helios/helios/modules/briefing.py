"""Helios v5 — Briefing Module.

Generates structured morning and evening briefings that get pushed to
Discord as rich embeds. The morning briefing covers today's calendar,
sleep, nutrition, habits, and weather. The evening debrief covers
accomplishments, habit check-ins, mood, and wind-down suggestions.

Architecture:
    - generate_morning() and generate_evening() are the public entry points
    - Each reads from the context table and module-specific tables
    - Output is a Matrix embed dict (ready for matrix_pusher.py)
    - Briefings are logged in briefing_log to prevent duplicates
    - Script-engine owned — zero LLM cost for standard briefings
    - An optional "insight of the day" can request LLM via llm_requests

Tables used:
    - briefing_log (created by migration 003)
    - context (created by migration 001)
    - calendar_events (created by migration 002)
    - mood (created by migration 001)
    - habit_log, habits (created by migration 001)
    - focus (created by migration 001)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("helios.briefing")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODULE_NAME = "briefing"
SOURCE = "script_engine"

# Embed color palette (matches config.yaml formatting.color_map)
COLOR_BRIEFING = 0x3498DB    # Blue — morning
COLOR_DEBRIEF = 0x2ECC71    # Green — evening
COLOR_ALERT   = 0xE74C3C    # Red — anomalies

# SQL to create briefing_log table if migration 003 hasn't run yet.
_BRIEFING_LOG_DDL = """
CREATE TABLE IF NOT EXISTS briefing_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    briefing_type   TEXT NOT NULL CHECK (briefing_type IN ('morning', 'evening')),
    sent_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    date_key        TEXT NOT NULL,          -- YYYY-MM-DD — prevents duplicate per day
    content_hash    TEXT,                   -- Hash of content for dedup
    discord_msg_id  TEXT,                   -- Discord message ID after send
    status          TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'queued', 'failed')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT uq_briefing_type_date UNIQUE (briefing_type, date_key)
);
CREATE INDEX IF NOT EXISTS idx_briefing_log_date ON briefing_log (date_key);
"""

# Context UPSERT
_CONTEXT_UPSERT_SQL = """
INSERT INTO context (source, module, key, value, priority)
VALUES (:source, :module, :key, :value, :priority)
ON CONFLICT (module, key, source) DO UPDATE SET
    value   = excluded.value,
    ts      = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    priority = excluded.priority
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today_key() -> str:
    """Return YYYY-MM-DD for today in UTC."""
    return _now_utc().strftime("%Y-%m-%d")


def _parse_iso8601(dt_str: str) -> datetime:
    """Parse an ISO8601 datetime string, returning timezone-aware UTC."""
    if not dt_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            dt = datetime.strptime(dt_str[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _format_time(iso_str: str) -> str:
    """Format an ISO8601 time string as 'HH:MM' in local-friendly format."""
    dt = _parse_iso8601(iso_str)
    return dt.strftime("%H:%M")


def _progress_bar(current: float, target: float, width: int = 10) -> str:
    """Generate an ASCII progress bar.

    Examples:
        _progress_bar(0.5, 1.0)  -> '█████░░░░░'
        _progress_bar(75, 100)   -> '███████░░░'
    """
    if target <= 0:
        return "░" * width
    ratio = min(current / target, 1.0)
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# BriefingModule
# ---------------------------------------------------------------------------

from .base import BaseMod

class BriefingModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "briefing",
        "version": "1.0.0",
        "description": "Generates morning briefings and evening debriefs",
        "author": "system",
        "collectors": [],
        "dependencies": [],
        "priority": 90,
    }

    """Helios v5 Briefing Module.

    Generates structured morning and evening Matrix embed briefings from
    Helios context and module tables.

    Parameters:
        db_path: Path to the SQLite database.
        config: Module configuration dict. Expected keys:
            - morning_time (str): Default '07:00'
            - evening_time (str): Default '21:00'
            - timezone (str, optional): IANA timezone name
            - format (str): 'short' or 'full'
            - enabled (bool): Whether the module is active
            - insight_enabled (bool): Whether to request LLM insight
    """

    def __init__(self, db_path: str, config: dict) -> None:
        super().__init__(db_path=db_path, config=config)
        self.db_path = db_path
        self.config = config or {}
        self._morning_time: str = self.config.get("morning_time", "07:00")
        self._evening_time: str = self.config.get("evening_time", "21:00")
        self._timezone: str | None = self.config.get("timezone")
        self._format: str = self.config.get("format", "short")
        self._insight_enabled: bool = self.config.get("insight_enabled", True)
        self._ensure_tables()

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_tables(self) -> None:
        try:
            conn = self._get_conn()
            conn.executescript(_BRIEFING_LOG_DDL)
            conn.close()
        except sqlite3.Error as exc:
            logger.error("Failed to ensure briefing_log table: %s", exc)

    def _already_sent_today(self, briefing_type: str) -> bool:
        """Check if a briefing of the given type was already sent today."""
        today = _today_key()
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM briefing_log WHERE briefing_type = ? AND date_key = ?",
                (briefing_type, today),
            ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def _log_briefing(self, briefing_type: str, status: str = "sent") -> None:
        """Record that a briefing was sent today."""
        today = _today_key()
        conn = self._get_conn()
        try:
            conn.execute(
                """
                INSERT INTO briefing_log (briefing_type, date_key, status)
                VALUES (?, ?, ?)
                ON CONFLICT (briefing_type, date_key) DO UPDATE SET
                    sent_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    status = excluded.status
                """,
                (briefing_type, today, status),
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("Error logging briefing: %s", exc)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Context query helpers
    # ------------------------------------------------------------------

    def _read_context_value(self, conn: sqlite3.Connection, key: str) -> Any:
        """Read a single context value by key, returning the JSON-decoded value."""
        row = conn.execute(
            "SELECT value FROM context WHERE key = ? ORDER BY ts DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    def _read_context_values(self, conn: sqlite3.Connection, prefix: str) -> dict[str, Any]:
        """Read all context values whose key starts with the given prefix.

        Returns dict of {short_key: value} where short_key strips the prefix.
        E.g., prefix='calendar.' -> {'busy_today': True, 'today_event_count': 3}
        """
        rows = conn.execute(
            "SELECT key, value FROM context WHERE key LIKE ? ORDER BY ts DESC",
            (f"{prefix}%",),
        ).fetchall()

        result: dict[str, Any] = {}
        for row in rows:
            short_key = row["key"][len(prefix):]
            if short_key not in result:
                try:
                    result[short_key] = json.loads(row["value"])
                except (json.JSONDecodeError, TypeError):
                    result[short_key] = row["value"]
        return result

    def _get_today_calendar_events(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """Return today's calendar events from the calendar_events table."""
        now = _now_utc()
        start_str = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        end_str = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()

        try:
            rows = conn.execute(
                """
                SELECT title, location, start_time, end_time, is_all_day, busy_free
                FROM calendar_events
                WHERE start_time >= ? AND start_time < ?
                ORDER BY start_time ASC
                """,
                (start_str, end_str),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []

    def _get_today_mood_entries(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """Return today's mood check-in entries."""
        today = _today_key()
        try:
            rows = conn.execute(
                """
                SELECT emoji, score, note, ts
                FROM mood
                WHERE date(ts) = date(?)
                ORDER BY ts ASC
                """,
                (today,),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []

    def _get_habit_status(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """Return all habits with today's completion status."""
        today = _today_key()
        try:
            rows = conn.execute(
                """
                SELECT h.slug, h.description, h.current_streak, h.longest_streak,
                       h.last_completed,
                       EXISTS(SELECT 1 FROM habit_log hl
                              WHERE hl.habit_id = h.id AND date(hl.ts) = date(?)) AS done_today
                FROM habits h
                ORDER BY h.slug
                """,
                (today,),
            ).fetchall()
            return [
                {
                    "slug": row["slug"],
                    "description": row["description"],
                    "current_streak": row["current_streak"],
                    "longest_streak": row["longest_streak"],
                    "done_today": bool(row["done_today"]),
                }
                for row in rows
            ]
        except sqlite3.Error:
            return []

    def _get_focus_summary(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Return today's focus session summary."""
        today = _today_key()
        try:
            rows = conn.execute(
                """
                SELECT state, SUM(
                    CASE WHEN duration_secs IS NOT NULL THEN duration_secs
                         WHEN session_start IS NOT NULL AND session_end IS NOT NULL
                         THEN (julianday(session_end) - julianday(session_start)) * 86400
                         ELSE 0 END
                ) AS total_secs
                FROM focus
                WHERE date(ts) = date(?)
                GROUP BY state
                """,
                (today,),
            ).fetchall()
            return {row["state"]: row["total_secs"] or 0 for row in rows}
        except sqlite3.Error:
            return {}

    # ------------------------------------------------------------------
    # Morning briefing
    # ------------------------------------------------------------------

    def generate_morning(self) -> dict[str, Any]:
        """Generate the morning briefing as a Matrix embed dict.

        Sections:
            1. Calendar — today's events, free blocks, all-day events
            2. Weather — current conditions from context
            3. Habits — streak status and which are done
            4. Nutrition — protein/hydration from context
            5. Anomalies — any flagged items
            6. Watch For — proactive suggestions based on calendar + patterns

        Returns:
            Dict with:
                - embed: Matrix embed dict
                - already_sent: bool (True if briefing was already sent today)
                - content: dict of section data for logging/testing
        """
        if self._already_sent_today("morning"):
            return {"embed": None, "already_sent": True, "content": {}}

        conn = self._get_conn()
        try:
            content: dict[str, Any] = {}

            # --- Calendar Section ---
            cal_ctx = self._read_context_values(conn, "calendar.")
            events = self._get_today_calendar_events(conn)
            content["calendar"] = {
                "busy_today": cal_ctx.get("busy_today", False),
                "event_count": cal_ctx.get("today_event_count", 0),
                "has_all_day": cal_ctx.get("has_all_day_event", False),
                "free_block_minutes": cal_ctx.get("free_block_minutes", 0),
                "next_event": cal_ctx.get("next_event_title", ""),
                "next_event_in": cal_ctx.get("event_coming_in_minutes"),
                "events": events,
            }

            # --- Weather Section ---
            weather = self._read_context_values(conn, "weather.")
            content["weather"] = weather

            # --- Habits Section ---
            habits = self._get_habit_status(conn)
            content["habits"] = habits

            # --- Nutrition Section ---
            protein = self._read_context_values(conn, "protein.")
            content["nutrition"] = protein

            # --- Anomalies Section ---
            anomalies = self._read_context_values(conn, "system.")
            content["anomalies"] = anomalies

        finally:
            conn.close()

        # --- Build Discord Embed ---
        embed = self._build_morning_embed(content)

        return {"embed": embed, "already_sent": False, "content": content}

    def _build_morning_embed(self, content: dict[str, Any]) -> dict[str, Any]:
        """Build the Matrix embed for the morning briefing."""
        fields: list[dict[str, Any]] = []

        # Calendar section
        cal = content.get("calendar", {})
        event_count = cal.get("event_count", 0)
        if event_count > 0:
            cal_lines = []
            for evt in cal.get("events", [])[:8]:
                time_str = _format_time(evt.get("start_time", "")) if not evt.get("is_all_day") else "All Day"
                title = evt.get("title", "?")
                busy = "🔴" if evt.get("busy_free") == "busy" else "🟢"
                cal_lines.append(f"{busy} {time_str} — {title}")

            events_label = f"📅 Today's Events ({event_count})"
            fields.append({
                "name": events_label,
                "value": "\n".join(cal_lines) or "No events",
                "inline": False,
            })

            # Next event
            next_in = cal.get("next_event_in")
            next_title = cal.get("next_event", "")
            if next_in is not None and next_title:
                fields.append({
                    "name": "⏰ Next Up",
                    "value": f"**{next_title}** in {next_in} min",
                    "inline": True,
                })

            # Free time
            free_min = cal.get("free_block_minutes", 0)
            if free_min > 0:
                hours = free_min // 60
                mins = free_min % 60
                free_str = f"{hours}h {mins}m" if hours else f"{mins}m"
                fields.append({
                    "name": "🟢 Free Time",
                    "value": f"Longest block: {free_str}",
                    "inline": True,
                })
        else:
            fields.append({
                "name": "📅 Today's Calendar",
                "value": "No events — free day!",
                "inline": False,
            })

        # All-day events
        if cal.get("has_all_day"):
            all_day_events = [
                e for e in cal.get("events", []) if e.get("is_all_day")
            ]
            if all_day_events:
                all_day_names = ", ".join(e.get("title", "?") for e in all_day_events)
                fields.append({
                    "name": "📌 All-Day",
                    "value": all_day_names,
                    "inline": False,
                })

        # Weather section
        weather = content.get("weather", {})
        if weather:
            temp = weather.get("current_temp", weather.get("temp"))
            conditions = weather.get("conditions", weather.get("summary", ""))
            if temp is not None:
                weather_str = f"🌡️ {temp}°C"
                if conditions:
                    weather_str += f" — {conditions}"
                fields.append({
                    "name": "🌤️ Weather",
                    "value": weather_str,
                    "inline": True,
                })

        # Habits section
        habits = content.get("habits", [])
        if habits:
            habit_lines = []
            for h in habits:
                status = "✅" if h.get("done_today") else "⬜"
                streak = h.get("current_streak", 0)
                desc = h.get("description", h.get("slug", ""))
                streak_str = f" (🔥{streak})" if streak > 1 else ""
                habit_lines.append(f"{status} {desc}{streak_str}")
            fields.append({
                "name": "💪 Habits",
                "value": "\n".join(habit_lines),
                "inline": False,
            })

        # Nutrition section
        nutrition = content.get("nutrition", {})
        if nutrition:
            protein_current = nutrition.get("current_grams", nutrition.get("today_grams"))
            protein_target = nutrition.get("target_grams")
            if protein_current is not None:
                bar = _progress_bar(
                    protein_current,
                    protein_target or 150,
                )
                target_str = f"/{protein_target}g" if protein_target else "g"
                fields.append({
                    "name": "🥩 Protein",
                    "value": f"`{bar}` {protein_current}{target_str}",
                    "inline": True,
                })

            hydration = nutrition.get("water_ml", nutrition.get("today_water_ml"))
            if hydration is not None:
                fields.append({
                    "name": "💧 Hydration",
                    "value": f"{hydration} ml",
                    "inline": True,
                })

        # Watch For section
        watch_lines = self._generate_watch_for(content)
        if watch_lines:
            fields.append({
                "name": "👀 Watch For",
                "value": "\n".join(watch_lines),
                "inline": False,
            })

        # Top correlations from correlation engine
        try:
            from helios.correlator import CorrelationEngine
            correlator = CorrelationEngine(db_path=self.db_path, config={})
            corrs = correlator.get_top_correlations(limit=3, min_strength="moderate")
            if corrs:
                corr_text = correlator.format_briefing_section(corrs)
                fields.append({
                    "name": "🔗 Patterns Detected",
                    "value": corr_text,
                    "inline": False,
                })
        except Exception:
            logger.debug("Correlation engine not available for morning briefing")

        embed = {
            "title": "☀️ Morning Briefing",
            "description": "Good morning! Here's your day at a glance.",
            "color": COLOR_BRIEFING,
            "fields": fields,
            "footer": {"text": "Helios v6"},
            "timestamp": _now_utc().isoformat(),
        }

        return embed

    def _generate_watch_for(self, content: dict[str, Any]) -> list[str]:
        """Generate proactive 'watch for' suggestions from content data."""
        lines: list[str] = []
        cal = content.get("calendar", {})

        if cal.get("busy_today") and cal.get("next_event_in") is not None:
            next_in = cal["next_event_in"]
            if isinstance(next_in, (int, float)) and next_in <= 60:
                lines.append("⏰ Early meeting coming up — prep now")

        free_min = cal.get("free_block_minutes", 0)
        if isinstance(free_min, (int, float)) and free_min >= 120:
            hours = free_min // 60
            lines.append(f"🧠 {hours}h+ free block — great for deep work")

        if cal.get("has_all_day"):
            lines.append("📌 All-day event today — plan around it")

        if not cal.get("busy_today") and cal.get("event_count", 0) == 0:
            lines.append("🌿 No meetings — protect this day for focus")

        return lines

    # ------------------------------------------------------------------
    # Evening debrief
    # ------------------------------------------------------------------

    def generate_evening(self) -> dict[str, Any]:
        """Generate the evening debrief as a Matrix embed dict.

        Sections:
            1. Accomplished — what went well today
            2. Habit Check-in — which done, which missed
            3. Mood Trend — mood entries through the day
            4. Focus Summary — time spent in each state
            5. Wind-down — suggested bedtime if sleep target not met

        Returns:
            Dict with:
                - embed: Matrix embed dict
                - already_sent: bool
                - content: dict of section data
        """
        if self._already_sent_today("evening"):
            return {"embed": None, "already_sent": True, "content": {}}

        conn = self._get_conn()
        try:
            content: dict[str, Any] = {}

            cal_ctx = self._read_context_values(conn, "calendar.")
            events = self._get_today_calendar_events(conn)
            content["calendar"] = {
                "event_count": cal_ctx.get("today_event_count", 0),
                "busy_today": cal_ctx.get("busy_today", False),
                "events": events,
            }

            mood_entries = self._get_today_mood_entries(conn)
            content["mood"] = mood_entries

            habits = self._get_habit_status(conn)
            content["habits"] = habits

            focus = self._get_focus_summary(conn)
            content["focus"] = focus

            nutrition = self._read_context_values(conn, "protein.")
            content["nutrition"] = nutrition

        finally:
            conn.close()

        embed = self._build_evening_embed(content)

        return {"embed": embed, "already_sent": False, "content": content}

    def _build_evening_embed(self, content: dict[str, Any]) -> dict[str, Any]:
        """Build the Matrix embed for the evening debrief."""
        fields: list[dict[str, Any]] = []

        # Calendar recap
        cal = content.get("calendar", {})
        event_count = cal.get("event_count", 0)
        if event_count > 0:
            completed = len([e for e in cal.get("events", []) if e.get("busy_free") == "busy"])
            fields.append({
                "name": "📊 Today's Recap",
                "value": f"📅 {event_count} events | {completed} busy blocks",
                "inline": False,
            })
        else:
            fields.append({
                "name": "📊 Today's Recap",
                "value": "No calendar events today",
                "inline": False,
            })

        # Habit check-in
        habits = content.get("habits", [])
        if habits:
            done = [h for h in habits if h.get("done_today")]
            missed = [h for h in habits if not h.get("done_today")]
            habit_lines = []
            for h in done:
                streak = h.get("current_streak", 0)
                desc = h.get("description", h.get("slug", ""))
                habit_lines.append(f"✅ {desc} (🔥{streak})")
            for h in missed:
                desc = h.get("description", h.get("slug", ""))
                habit_lines.append(f"❌ {desc}")
            fields.append({
                "name": f"💪 Habits ({len(done)}/{len(habits)})",
                "value": "\n".join(habit_lines),
                "inline": False,
            })

        # Mood trend
        mood_entries = content.get("mood", [])
        if mood_entries:
            mood_line = " → ".join(
                f"{e.get('emoji', '?')}" for e in mood_entries
            )
            scores = [e.get("score", 0) for e in mood_entries if e.get("score")]
            avg = sum(scores) / len(scores) if scores else 0
            fields.append({
                "name": "😊 Mood Trend",
                "value": f"{mood_line} (avg: {avg:.1f}/10)",
                "inline": False,
            })

        # Focus summary
        focus = content.get("focus", {})
        if focus:
            focus_lines = []
            state_icons = {
                "working": "💻",
                "gaming": "🎮",
                "meeting": "📞",
                "idle": "⏸️",
                "break": "☕",
            }
            for state, secs in sorted(focus.items(), key=lambda x: -x[1]):
                if secs > 0:
                    hours = secs / 3600
                    icon = state_icons.get(state, "❓")
                    if hours >= 1:
                        focus_lines.append(f"{icon} {state.title()}: {hours:.1f}h")
                    else:
                        focus_lines.append(f"{icon} {state.title()}: {secs // 60}m")
            if focus_lines:
                fields.append({
                    "name": "🎯 Focus",
                    "value": "\n".join(focus_lines[:6]),
                    "inline": False,
                })

        # Nutrition recap
        nutrition = content.get("nutrition", {})
        if nutrition:
            protein = nutrition.get("current_grams", nutrition.get("today_grams"))
            target = nutrition.get("target_grams")
            if protein is not None:
                bar = _progress_bar(protein, target or 150)
                pct = int((protein / (target or 150)) * 100) if target else 0
                fields.append({
                    "name": "🥩 Protein",
                    "value": f"`{bar}` {protein}/{target or '?'}g ({pct}%)",
                    "inline": True,
                })

        # Wind-down suggestion
        wind_down = self._generate_wind_down(content)
        if wind_down:
            fields.append({
                "name": "🌙 Wind-Down",
                "value": wind_down,
                "inline": False,
            })

        embed = {
            "title": "🌙 Evening Debrief",
            "description": "Here's how your day went.",
            "color": COLOR_DEBRIEF,
            "fields": fields,
            "footer": {"text": "Helios v6"},
            "timestamp": _now_utc().isoformat(),
        }

        return embed

    def _generate_wind_down(self, content: dict[str, Any]) -> str:
        """Generate a wind-down suggestion."""
        cal = content.get("calendar", {})

        if cal.get("busy_today"):
            return "Busy day — consider winding down by 11pm for recovery."

        focus = content.get("focus", {})
        gaming_secs = focus.get("gaming", 0)
        if gaming_secs > 7200:
            return "🎮 Long gaming session today — screen break before bed?"

        mood_entries = content.get("mood", [])
        if mood_entries:
            latest_score = mood_entries[-1].get("score", 10)
            if latest_score <= 3:
                return "💙 Rough day — be kind to yourself tonight."

        return "Good day! Time to recharge for tomorrow."

    # ------------------------------------------------------------------
    # Main tick method
    # ------------------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        """Main scheduler entry point. Determines which briefing to generate.

        Called on each scheduler interval. Checks the current time against
        configured morning/evening times, generates the appropriate briefing
        if it hasn't been sent today, and logs it.

        Returns:
            Dict with:
                - generated (str): 'morning', 'evening', or 'none'
                - sent (bool): Whether a briefing was actually sent
                - already_sent (bool): Whether it was already sent today
        """
        now = _now_utc()
        current_time = now.strftime("%H:%M")

        result: dict[str, Any] = {
            "generated": "none",
            "sent": False,
            "already_sent": False,
        }

        if current_time >= self._morning_time and current_time < self._evening_time:
            briefing = self.generate_morning()
            result["generated"] = "morning"
            if briefing["already_sent"]:
                result["already_sent"] = True
            elif briefing["embed"]:
                self._log_briefing("morning")
                result["sent"] = True

        elif current_time >= self._evening_time:
            briefing = self.generate_evening()
            result["generated"] = "evening"
            if briefing["already_sent"]:
                result["already_sent"] = True
            elif briefing["embed"]:
                self._log_briefing("evening")
                result["sent"] = True

        return result

    # ------------------------------------------------------------------
    # Utility / diagnostics
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return module status for health checks."""
        return {
            "module": MODULE_NAME,
            "morning_time": self._morning_time,
            "evening_time": self._evening_time,
            "format": self._format,
            "insight_enabled": self._insight_enabled,
            "db_path": self.db_path,
        }