"""Helios v5 — Calendar Module.

Reads iCloud calendar data via pyicloud and feeds it into the Helios rules
engine for proactive suggestions. Falls back gracefully to cached SQLite
data when pyicloud is unavailable.

Architecture:
    - tick() is called by the scheduler on each interval
    - tick() fetches calendar data and upserts into calendar_events
    - tick() writes computed context keys to the context table
    - Rules engine reads from context table to evaluate rules
    - All database access goes through db_path parameter
    - No LLM needed — this is deterministic/script-engine owned

Context keys written:
    calendar.busy_today          (bool)   — True if any busy event today
    calendar.free_block_minutes (int)    — Longest free block today in minutes
    calendar.event_coming_in_minutes (int|None) — Minutes until next event
    calendar.has_all_day_event  (bool)   — True if any all-day event today
    calendar.next_event_title   (str)    — Sanitized title of next event
    calendar.next_event_start   (str)    — ISO8601 start of next event
    calendar.today_event_count  (int)    — Number of events today

Tables used:
    - calendar_events (created by migration 002)
    - context (created by migration 001, UNIQUE on module+key+source)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

# Optional dependency — pyicloud may not be installed
try:
    from pyicloud import PyiCloudService  # type: ignore[import-untyped]
    from pyicloud.exceptions import PyiCloudFailedLoginException  # type: ignore[import-untyped]

    PYICLOUD_AVAILABLE = True
except ImportError:
    PYICLOUD_AVAILABLE = False

logger = logging.getLogger("helios.calendar")

# Try to import HA client for calendar events; gracefully degrade if unavailable
try:
    from ..ha_client import fetch_calendar_events as _ha_fetch_calendar
    HA_CLIENT_AVAILABLE = True
except ImportError:
    HA_CLIENT_AVAILABLE = False

    def _ha_fetch_calendar(*args, **kwargs):  # type: ignore[misc]
        return []

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODULE_NAME = "calendar"
SOURCE = "script_engine"

# SQL to create calendar_events table if migration 002 hasn't run yet.
# Kept as a safety net — normally the migration handles this.
_CALENDAR_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS calendar_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    icloud_id       TEXT UNIQUE,
    title           TEXT NOT NULL,
    location        TEXT,
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    is_all_day      INTEGER NOT NULL DEFAULT 0,
    busy_free       TEXT NOT NULL DEFAULT 'busy',
    source          TEXT NOT NULL DEFAULT 'pyicloud',
    ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_cal_events_start ON calendar_events (start_time);
CREATE INDEX IF NOT EXISTS idx_cal_events_icloud ON calendar_events (icloud_id) WHERE icloud_id IS NOT NULL;
"""

# Context UPSERT — leverages the UNIQUE(module, key, source) constraint
_CONTEXT_UPSERT_SQL = """
INSERT INTO context (source, module, key, value, priority)
VALUES (:source, :module, :key, :value, :priority)
ON CONFLICT (module, key, source) DO UPDATE SET
    value   = excluded.value,
    ts      = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    priority = excluded.priority
"""

# Calendar events upsert — leverages UNIQUE(icloud_id)
_EVENT_UPSERT_SQL = """
INSERT INTO calendar_events (icloud_id, title, location, start_time, end_time,
                             is_all_day, busy_free, source)
VALUES (:icloud_id, :title, :location, :start_time, :end_time,
        :is_all_day, :busy_free, :source)
ON CONFLICT (icloud_id) DO UPDATE SET
    title      = excluded.title,
    location   = excluded.location,
    start_time = excluded.start_time,
    end_time   = excluded.end_time,
    is_all_day = excluded.is_all_day,
    busy_free  = excluded.busy_free,
    source     = excluded.source,
    ts         = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
"""

# Sensitive patterns to strip from event titles before storing
_SENSITIVE_PATTERN = re.compile(
    r"(password|ssn|social.security|credit.card|api.key|secret|token|credential)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deterministic_event_id(entity_id: str, start_time: str, end_time: str, title: str) -> str:
    """Generate a deterministic event ID from HA event data.

    Uses a stable SHA-256 hash of the inputs to produce a consistent
    identifier (same inputs always yield the same ID).  Returns a string
    prefixed with ``ha_`` so HA-sourced events are easy to distinguish
    from iCloud-sourced ones.

    Args:
        entity_id: HA calendar entity ID (e.g. ``calendar.work``).
        start_time: ISO-8601 start time string.
        end_time: ISO-8601 end time string.
        title: Sanitized event title.

    Returns:
        Deterministic identifier like ``ha_a1b2c3d4e5f6g7h8``.
    """
    import hashlib

    raw = f"{entity_id}|{start_time}|{end_time}|{title}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"ha_{digest}"


def _sanitize_title(raw_title: str) -> str:
    """Sanitize an event title.

    - Strips leading/trailing whitespace
    - Replaces obvious sensitive terms with [REDACTED]
    - Caps title length at 200 chars
    """
    title = raw_title.strip()
    if _SENSITIVE_PATTERN.search(title):
        title = _SENSITIVE_PATTERN.sub("[REDACTED]", title)
    return title[:200] if len(title) > 200 else title


def _parse_iso8601(dt_str: str) -> datetime:
    """Parse an ISO8601 datetime string, handling multiple common formats.

    Accepts:
        - 2024-01-15T09:30:00+00:00
        - 2024-01-15T09:30:00Z
        - 2024-01-15T09:30:00
        - 2024-01-15 (date only — midnight local)

    Returns a timezone-aware datetime in UTC.
    """
    if not dt_str:
        return datetime.min.replace(tzinfo=timezone.utc)

    # Try the standard fromisoformat approach (Python 3.11+ handles Z suffix)
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        # Fallback: date only
        try:
            dt = datetime.strptime(dt_str[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            logger.warning("Could not parse datetime: %r — using epoch", dt_str)
            return datetime.min.replace(tzinfo=timezone.utc)

    # Ensure timezone-aware (assume UTC if naive)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt


def _now_utc() -> datetime:
    """Return current UTC-aware datetime."""
    return datetime.now(timezone.utc)


def _today_range_utc(tz_name: str | None = None) -> tuple[datetime, datetime]:
    """Return (start_of_day, end_of_day) in UTC for today's date.

    If tz_name is provided (IANA timezone like 'America/Edmonton'), day
    boundaries use that timezone converted to UTC.  Falls back to the
    system local timezone when tz_name is None.

    The range also extends 1 day into the future so that "upcoming event"
    queries (e.g. an event 30 minutes from now that crosses midnight) are
    not missed when the server runs in a non-UTC timezone.
    """
    try:
        from zoneinfo import ZoneInfo
        if tz_name:
            tz = ZoneInfo(tz_name)
        else:
            tz = timezone.utc
    except (ImportError, KeyError):
        tz = timezone.utc

    # Get "today" in the user's local timezone
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Convert to UTC
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    # Extend end by 1 day to catch events that start "tomorrow" in local time
    # but are relevant for near-future context (e.g. 30 min from now after midnight)
    end_utc_extended = end_utc + timedelta(days=1)

    return start_utc, end_utc_extended


# ---------------------------------------------------------------------------
# CalendarModule
# ---------------------------------------------------------------------------


from .base import BaseMod

class CalendarModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "calendar",
        "version": "1.0.0",
        "description": "Syncs iCloud calendar events",
        "author": "system",
        "collectors": ['calendar_cache.json'],
        "dependencies": ['pyicloud'],
        "priority": 2,
    }

    """Helios v5 Calendar Module.

    Reads iCloud calendar data (via pyicloud) and feeds it into the Helios
    rules engine through the context table. Falls back to cached SQLite data
    when pyicloud is unavailable.

    Parameters:
        db_path: Path to the SQLite database.
        config: Module configuration dict. Expected keys:
            - apple_id (str): iCloud Apple ID
            - password (str): iCloud app-specific password
            - enabled (bool): Whether the module is active
            - interval (int): Tick interval in seconds
            - timezone (str, optional): IANA timezone name (default: 'local')
    """

    def __init__(self, db_path: str, config: dict) -> None:
        super().__init__(db_path=db_path, config=config)
        self.db_path = db_path
        self.config = config or {}
        self._apple_id: str = self.config.get("apple_id", "")
        self._password: str = self.config.get("password", "")
        self._timezone: str | None = self.config.get("timezone")  # IANA tz name
        self._icloud_service: Any = None

        # Home Assistant calendar config
        self._ha_enabled: bool = self.config.get("ha_enabled", True)
        self._ha_base_url: str = self.config.get("ha_base_url", "")
        self._ha_token: str = self.config.get("ha_token", "") or os.environ.get("HASS_TOKEN", "") or os.environ.get("HA_TOKEN", "")
        self._ha_calendars: list[str] = self.config.get("ha_calendars", [])
        self._fallback_enabled: bool = self.config.get("fallback_enabled", True)

        # Default HA base URL from global home_assistant if not set locally
        if self._ha_enabled and not self._ha_base_url:
            # Try to read global from environment or a known default
            self._ha_base_url = os.environ.get("HASS_URL", "") or os.environ.get("HOME_ASSISTANT_URL", "") or ""

        self._ensure_tables()

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Get a new SQLite connection with WAL mode and row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_tables(self) -> None:
        """Create calendar_events table if it doesn't exist yet.

        Normally created by migration 002, but this is a safety net so
        the module can run standalone or during development before
        the full migration suite is applied.
        """
        try:
            conn = self._get_conn()
            conn.executescript(_CALENDAR_EVENTS_DDL)
            conn.close()
        except sqlite3.Error as exc:
            logger.error("Failed to ensure calendar_events table: %s", exc)

    # ------------------------------------------------------------------
    # Home Assistant calendar integration
    # ------------------------------------------------------------------

    def _fetch_ha_events(self) -> list[dict[str, Any]]:
        """Fetch events from all configured Home Assistant calendar entities.

        Returns a list of event dicts with keys matching calendar_events columns.
        Returns an empty list if HA is not configured, unavailable, or returns errors.
        """
        if not HA_CLIENT_AVAILABLE:
            logger.debug("HA client not available — skipping HA calendar fetch")
            return []
        if not self._ha_enabled or not self._ha_base_url or not self._ha_token or not self._ha_calendars:
            logger.debug("HA calendar not configured — skipping")
            return []

        now = _now_utc()
        from_dt = now
        to_dt = now + timedelta(days=7)

        all_events: list[dict[str, Any]] = []
        for entity_id in self._ha_calendars:
            try:
                raw_events = _ha_fetch_calendar(
                    base_url=self._ha_base_url,
                    token=self._ha_token,
                    entity_id=entity_id,
                    start=from_dt,
                    end=to_dt,
                    timeout=15,
                )
                for evt in raw_events:
                    normalized = self._normalize_ha_event(evt, entity_id)
                    if normalized:
                        all_events.append(normalized)
            except Exception as exc:
                logger.warning("HA calendar fetch failed for %s: %s", entity_id, exc)

        if all_events:
            logger.info("Fetched %d events from HA calendars", len(all_events))
        return all_events

    def _normalize_ha_event(self, evt: dict[str, Any], entity_id: str) -> dict[str, Any] | None:
        """Normalize a raw HA calendar event into a DB-ready dict.

        Handles two HA event formats:
        1. HA entity format: {"title", "start_time", "end_time", "is_all_day"}
        2. Google Calendar via HA: {"summary", "start": {"dateTime"/"date"}, ...}

        Uses the HA event 'uid' as the unique key (stored in the icloud_id column
        for schema compatibility).  Returns None if normalization fails.
        """
        try:
            # Title: HA entity uses "title", Google Calendar uses "summary"
            raw_title = evt.get("title") or evt.get("summary", "") or "Untitled Event"
            title = _sanitize_title(raw_title)

            # Extract start/end times from either format
            # Google Calendar via HA: {"start": {"dateTime": "..."} or {"date": "..."}}
            start_obj = evt.get("start", {})
            end_obj = evt.get("end", {})
            if isinstance(start_obj, dict):
                start_time = start_obj.get("dateTime") or start_obj.get("date", "")
                end_time = end_obj.get("dateTime") or end_obj.get("date", "")
                is_all_day = 1 if "date" in start_obj and "dateTime" not in start_obj else 0
            else:
                # HA entity format: flat keys
                start_time = evt.get("start_time", "")
                end_time = evt.get("end_time", "")
                is_all_day = 1 if evt.get("is_all_day", 0) == 1 else 0

            # Location: HA entity uses "location", Google Calendar uses "location"
            location = str(evt.get("location", "") or "")

            # UID
            uid = evt.get("uid")
            if not uid:
                uid = _deterministic_event_id(entity_id, start_time, end_time, title)

            if not start_time or not end_time:
                return None

            return {
                "icloud_id": str(uid),
                "title": title,
                "location": location,
                "start_time": start_time,
                "end_time": end_time,
                "is_all_day": is_all_day,
                "busy_free": "busy",
                "source": "home_assistant",
            }
        except Exception as exc:
            logger.warning("Error normalizing HA event from %s: %s", entity_id, exc)
            return None

    # ------------------------------------------------------------------
    # iCloud integration
    # ------------------------------------------------------------------

    def _get_icloud_service(self) -> Any:
        """Lazily initialize and return the shared pyicloud service.

        Uses the shared icloud_helper which handles cookie_directory auth.
        Returns None if pyicloud is not available or session is expired.
        """
        from .. import icloud_helper
        return icloud_helper.get_service()

    def _fetch_icloud_events(self) -> list[dict[str, Any]]:
        """Fetch events from iCloud for today and the next 7 days.

        Returns a list of event dicts with keys matching calendar_events columns.
        Returns an empty list on any failure (graceful degradation).
        """
        service = self._get_icloud_service()
        if service is None:
            return []

        events: list[dict[str, Any]] = []
        now = _now_utc()
        from_dt = now
        to_dt = now + timedelta(days=7)

        try:
            # pyicloud 2.5+ API: service.calendar (CalendarService) with
            # get_events() returning all events across all calendars.
            cal_service = service.calendar
            cal_events = cal_service.get_events(
                from_dt=from_dt, to_dt=to_dt, as_objs=True
            )
            for evt in cal_events:
                try:
                    evt_dict = self._normalize_icloud_event(evt)
                    if evt_dict:
                        events.append(evt_dict)
                except Exception as exc:
                    logger.warning(
                        "Error normalizing iCloud event %s: %s",
                        getattr(evt, "guid", "unknown"),
                        exc,
                    )
        except Exception as exc:
            logger.warning("Error fetching iCloud calendar events: %s", exc)

        return events

    def _normalize_icloud_event(self, evt: Any) -> dict[str, Any] | None:
        """Normalize a pyicloud event object into a dict for insertion.

        Handles pyicloud 2.5+ AppleCalendarEvent (dataclass with List[int]
        timestamps) and older attribute-based event objects.

        Returns None if the event cannot be normalized.
        """
        try:
            # pyicloud 2.5+ event attributes (dataclass fields)
            icloud_id = getattr(evt, "guid", None) or getattr(evt, "pguid", None)
            if not icloud_id:
                return None

            title = getattr(evt, "title", "") or "Untitled Event"
            location = getattr(evt, "location", "") or ""

            start_raw = getattr(evt, "startDate", None)
            end_raw = getattr(evt, "endDate", None)
            if not start_raw or not end_raw:
                return None

            # pyicloud 2.5+ returns List[int]: [Y, M, D, H, m, s]
            # Older versions return datetime or ISO8601 strings
            if isinstance(start_raw, list) and len(start_raw) >= 6:
                start_dt = datetime(*start_raw[:6], tzinfo=timezone.utc)
            elif isinstance(start_raw, list) and len(start_raw) >= 3:
                start_dt = datetime(*start_raw[:3], tzinfo=timezone.utc)
            elif isinstance(start_raw, datetime):
                start_dt = start_raw
            else:
                start_dt = _parse_iso8601(str(start_raw))

            if isinstance(end_raw, list) and len(end_raw) >= 6:
                end_dt = datetime(*end_raw[:6], tzinfo=timezone.utc)
            elif isinstance(end_raw, list) and len(end_raw) >= 3:
                end_dt = datetime(*end_raw[:3], tzinfo=timezone.utc)
            elif isinstance(end_raw, datetime):
                end_dt = end_raw
            else:
                end_dt = _parse_iso8601(str(end_raw))

            # Ensure timezone-aware (assume UTC if naive)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)

            # Detect all-day events
            is_all_day = 0
            all_day_flag = getattr(evt, "allDay", None)
            if all_day_flag:
                is_all_day = 1
            elif (
                start_dt.hour == 0
                and start_dt.minute == 0
                and end_dt.hour == 0
                and end_dt.minute == 0
                and (end_dt - start_dt).days >= 1
            ):
                is_all_day = 1

            busy_free = "busy"  # default
            # pyicloud 2.5+ may not have 'busy' — treat all as busy

            return {
                "icloud_id": str(icloud_id),
                "title": _sanitize_title(title),
                "location": str(location),
                "start_time": start_dt.isoformat(),
                "end_time": end_dt.isoformat(),
                "is_all_day": is_all_day,
                "busy_free": busy_free,
                "source": "pyicloud",
            }
        except Exception as exc:
            logger.warning("Error normalizing iCloud event: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Event persistence
    # ------------------------------------------------------------------

    def _sync_events_to_db(self, events: list[dict[str, Any]]) -> int:
        """Upsert a list of event dicts into the calendar_events table.

        Returns the number of events successfully upserted.
        """
        if not events:
            return 0

        upserted = 0
        conn = self._get_conn()
        try:
            for evt in events:
                try:
                    conn.execute(
                        _EVENT_UPSERT_SQL,
                        {
                            "icloud_id": evt.get("icloud_id"),
                            "title": evt.get("title", "Untitled Event"),
                            "location": evt.get("location"),
                            "start_time": evt.get("start_time"),
                            "end_time": evt.get("end_time"),
                            "is_all_day": evt.get("is_all_day", 0),
                            "busy_free": evt.get("busy_free", "busy"),
                            "source": evt.get("source", "pyicloud"),
                        },
                    )
                    upserted += 1
                except sqlite3.Error as exc:
                    logger.warning("Error upserting event %s: %s", evt.get("icloud_id"), exc)
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("Error committing event sync: %s", exc)
            conn.rollback()
        finally:
            conn.close()

        return upserted

    # ------------------------------------------------------------------
    # Query helpers (from SQLite cache)
    # ------------------------------------------------------------------

    def get_events_today(self) -> list[dict[str, Any]]:
        """Return today's events (and near-future events) from the calendar_events table.

        "Today" is defined as the user's local calendar date — all events whose
        start_time falls between midnight today and 23:59:59 tomorrow (local time),
        converted to UTC.  The extra day buffer ensures that "upcoming event"
        context keys work correctly near midnight.

        _compute_context() filters strictly-today events separately for
        today_event_count, has_all_day_event, and busy_today.

        Returns:
            A list of dicts with all calendar_events columns.
            Empty list on error.
        """
        start_of_day, end_of_day = _today_range_utc(self._timezone)
        start_str = start_of_day.isoformat()
        end_str = end_of_day.isoformat()

        conn = self._get_conn()
        try:
            # Query: events that OVERLAP with the local today range.
            # An event overlaps if it starts before the range ends AND ends after
            # the range starts. This correctly handles:
            #   - Timed events starting today
            #   - All-day events stored at UTC midnight that span into local today
            #   - Multi-day events that overlap with today
            rows = conn.execute(
                """
                SELECT id, icloud_id, title, location, start_time, end_time,
                       is_all_day, busy_free, source
                FROM calendar_events
                WHERE start_time >= ? AND start_time < ?
                ORDER BY start_time ASC
                """,
                (start_str, end_str),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            logger.error("Error querying today's events: %s", exc)
            return []
        finally:
            conn.close()

    def get_free_blocks_today(self) -> list[tuple[datetime, datetime, int]]:
        """Calculate free time blocks today between busy events.

        Finds the gaps between busy events (not 'free'-marked events) during
        waking hours (8:00–22:00 local). Returns a list of
        (start, end, duration_minutes) tuples for each free block.

        Returns:
            List of (start_dt, end_dt, minutes) tuples, sorted by start.
            Empty list on error or if no busy events.
        """
        start_of_day, end_of_day = _today_range_utc(self._timezone)
        now = _now_utc()

        conn = self._get_conn()
        try:
            # Get today's busy events (timed, not all-day) that overlap with
            # the local today range, ordered by start time
            rows = conn.execute(
                """
                SELECT start_time, end_time
                FROM calendar_events
                WHERE start_time >= ? AND start_time < ?
                  AND busy_free = 'busy'
                  AND is_all_day = 0
                ORDER BY start_time ASC
                """,
                (start_of_day.isoformat(), end_of_day.isoformat()),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.error("Error querying busy events for free blocks: %s", exc)
            return []
        finally:
            conn.close()

        # Define waking hours for free-block calculation
        waking_start = start_of_day.replace(hour=8, minute=0, second=0, microsecond=0)
        waking_end = start_of_day.replace(hour=22, minute=0, second=0, microsecond=0)

        # Free-block analysis starts from max(now, waking_start)
        cursor = max(now, waking_start)
        if cursor >= waking_end:
            # Day is already past waking hours
            return []

        free_blocks: list[tuple[datetime, datetime, int]] = []

        for row in rows:
            evt_start = _parse_iso8601(row["start_time"])
            evt_end = _parse_iso8601(row["end_time"])

            # Skip events that end before our cursor
            if evt_end <= cursor:
                continue

            # If there's a gap before this busy block, it's a free block
            if evt_start > cursor:
                gap_start = cursor
                gap_end = min(evt_start, waking_end)
                if gap_start < gap_end:
                    minutes = int((gap_end - gap_start).total_seconds() / 60)
                    free_blocks.append((gap_start, gap_end, minutes))

            # Advance cursor past this busy event
            cursor = max(cursor, evt_end)
            if cursor >= waking_end:
                break

        # Remaining time after last busy event until waking_end
        if cursor < waking_end:
            minutes = int((waking_end - cursor).total_seconds() / 60)
            if minutes > 0:
                free_blocks.append((cursor, waking_end, minutes))

        return free_blocks

    # ------------------------------------------------------------------
    # Context computation
    # ------------------------------------------------------------------

    def _is_today_event(self, evt: dict[str, Any]) -> bool:
        """Check if an event starts on the user's local 'today'.

        Uses _today_range_utc() boundaries so the 'today' definition is
        consistent with get_events_today().  For all-day events, the event
        overlaps with today if any part of it falls within the day range.
        """
        start_dt = _parse_iso8601(evt.get("start_time", ""))
        end_dt = _parse_iso8601(evt.get("end_time", ""))

        start_of_day, end_of_day_extended = _today_range_utc(self._timezone)
        # Strict end of day (no +1 day extension)
        end_of_day = start_of_day + timedelta(days=1)

        if evt.get("is_all_day", 0) == 1:
            # All-day events: the event overlaps with today if any part
            # of it falls within the day range.
            return start_dt < end_of_day and end_dt > start_of_day
        else:
            # Timed events: consider "today" if the event starts within
            # the day range (midnight to midnight in user's timezone).
            return start_of_day <= start_dt < end_of_day

    def _compute_context(self, events_today: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute all calendar context keys from today's events.

        Args:
            events_today: List of event dicts from get_events_today()
                (may include near-future events for upcoming detection).

        Returns:
            Dict mapping context key names to their computed values.
        """
        now = _now_utc()
        context: dict[str, Any] = {}

        # Separate strictly-today events from near-future events
        strictly_today = [e for e in events_today if self._is_today_event(e)]

        # --- calendar.today_event_count --- (strictly today only)
        context["today_event_count"] = len(strictly_today)

        # --- calendar.has_all_day_event --- (strictly today only)
        context["has_all_day_event"] = any(
            evt.get("is_all_day", 0) == 1 for evt in strictly_today
        )

        # --- calendar.busy_today --- (strictly today only)
        busy_events = [
            evt for evt in strictly_today
            if evt.get("busy_free", "busy") == "busy"
        ]
        context["busy_today"] = len(busy_events) > 0

        # --- calendar.free_block_minutes ---
        free_blocks = self.get_free_blocks_today()
        if free_blocks:
            context["free_block_minutes"] = max(block[2] for block in free_blocks)
        else:
            # No busy events today → entire waking window is free
            waking_start = now.replace(hour=8, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
            waking_end = now.replace(hour=22, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
            if now < waking_end:
                total_free = int((waking_end - max(now, waking_start)).total_seconds() / 60)
                context["free_block_minutes"] = total_free
            else:
                context["free_block_minutes"] = 0

        # --- Next upcoming event (not yet started or currently in progress) ---
        # Prefer timed (non-all-day) events that haven't ended yet.
        # All-day events are deprioritized since they're always "in progress."
        upcoming_all = []
        upcoming_timed = []
        for evt in events_today:
            start_dt = _parse_iso8601(evt.get("start_time", ""))
            end_dt = _parse_iso8601(evt.get("end_time", ""))
            if end_dt > now:
                entry = (start_dt, evt)
                upcoming_all.append(entry)
                if evt.get("is_all_day", 0) == 0:
                    upcoming_timed.append(entry)

        # Use timed events if available; fall back to all events (incl. all-day)
        upcoming = upcoming_timed if upcoming_timed else upcoming_all
        if upcoming:
            upcoming.sort(key=lambda x: x[0])
            next_start, next_evt = upcoming[0]
            context["next_event_title"] = next_evt.get("title", "")
            context["next_event_start"] = next_evt.get("start_time", "")
            minutes_until = max(0, int((next_start - now).total_seconds() / 60))
            # If event has already started (start < now), report 0
            context["event_coming_in_minutes"] = minutes_until
        else:
            context["next_event_title"] = ""
            context["next_event_start"] = ""
            context["event_coming_in_minutes"] = None

        return context

    def _write_context(self, conn: sqlite3.Connection, context_data: dict[str, Any]) -> None:
        """Write computed context values to the context table.

        Uses UPSERT on the UNIQUE(module, key, source) constraint so each
        key is always the latest value.

        Args:
            conn: Active SQLite connection (caller manages transaction).
            context_data: Dict from _compute_context().
        """
        # Map context key names to their tuple (key, value, priority)
        key_map: dict[str, tuple[str, Any, int]] = {
            "busy_today":             ("calendar.busy_today",             context_data["busy_today"],             0),
            "free_block_minutes":     ("calendar.free_block_minutes",     context_data["free_block_minutes"],     0),
            "event_coming_in_minutes":("calendar.event_coming_in_minutes",context_data["event_coming_in_minutes"], 1),
            "has_all_day_event":      ("calendar.has_all_day_event",      context_data["has_all_day_event"],      0),
            "next_event_title":      ("calendar.next_event_title",       context_data["next_event_title"],       0),
            "next_event_start":      ("calendar.next_event_start",       context_data["next_event_start"],       0),
            "today_event_count":     ("calendar.today_event_count",      context_data["today_event_count"],      0),
        }

        for _key_name, (full_key, value, priority) in key_map.items():
            # Serialize value as JSON
            json_value = json.dumps(value, default=str)
            conn.execute(
                _CONTEXT_UPSERT_SQL,
                {
                    "source": SOURCE,
                    "module": MODULE_NAME,
                    "key": full_key,
                    "value": json_value,
                    "priority": priority,
                },
            )

    # ------------------------------------------------------------------
    # Main tick method
    # ------------------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        """Main scheduler entry point. Called on each interval.

        Workflow (HA-first):
            1. Attempt to fetch Home Assistant calendar events
            2. If HA returns events, sync to DB (source=home_assistant)
            3. If HA returns nothing and fallback is enabled, try iCloud
            4. Query today's events from SQLite (works with HA or cached data)
            5. Compute context keys
            6. Write context keys to context table

        Returns:
            A dict with tick results and diagnostics:
                - sync_count (int): number of events synced
                - events_today (int): number of events today
                - context_written (int): number of context keys written
                - source (str): 'home_assistant', 'pyicloud', or 'cache'
                - fallback_used (bool): True if HA failed and iCloud was used
                - freshness_secs (int): age of newest calendar event in cache
        """
        result: dict[str, Any] = {
            "sync_count": 0,
            "events_today": 0,
            "context_written": 0,
            "source": "cache",
            "fallback_used": False,
        }

        # =====================================================================
        # Step 1 — Home Assistant (primary)
        # =====================================================================
        ha_events = self._fetch_ha_events()
        if ha_events:
            result["source"] = "home_assistant"
            result["sync_count"] = self._sync_events_to_db(ha_events)
            logger.info("Synced %d events from Home Assistant", result["sync_count"])

        # =====================================================================
        # Step 2 — iCloud fallback (only if HA failed and fallback enabled)
        # =====================================================================
        if not ha_events and self._fallback_enabled:
            icloud_events = self._fetch_icloud_events()
            if icloud_events:
                result["source"] = "pyicloud"
                result["fallback_used"] = True
                result["sync_count"] = self._sync_events_to_db(icloud_events)
                logger.info("Synced %d events from iCloud (fallback)", result["sync_count"])

        # =====================================================================
        # Step 3 — Always read from SQLite cache and compute context
        # =====================================================================
        events_today = self.get_events_today()
        result["events_today"] = len(events_today)
        context_data = self._compute_context(events_today)

        conn = self._get_conn()
        try:
            self._write_context(conn, context_data)
            conn.commit()
            result["context_written"] = len(context_data)
            logger.debug(
                "Wrote %d context keys for %d today-events",
                result["context_written"],
                result["events_today"],
            )
        except sqlite3.Error as exc:
            logger.error("Error writing calendar context: %s", exc)
            conn.rollback()
        finally:
            conn.close()

        # Freshness: track when we last successfully synced.
        # Any successful sync (events or empty) is fresh. Calendar events are
        # inherently slow-changing; only degrade if the *sync itself* fails.
        now = _now_utc()
        result["last_updated"] = now.isoformat()
        result["freshness_secs"] = 0
        result["_freshness_threshold_override"] = {
            "fresh": 72000,      # 20h
            "stale": 172800,     # 48h
            "degraded": 345600,  # 96h
        }

        return result

    # ------------------------------------------------------------------
    # Utility / diagnostics
    # ------------------------------------------------------------------

    def get_next_event(self) -> dict[str, Any] | None:
        """Convenience method: return the next upcoming event as a dict, or None.

        Prefers timed (non-all-day) events over all-day events.
        """
        events = self.get_events_today()
        now = _now_utc()
        upcoming_timed = []
        upcoming_all = []
        for evt in events:
            end_dt = _parse_iso8601(evt.get("end_time", ""))
            if end_dt > now:
                entry = (_parse_iso8601(evt["start_time"]), evt)
                upcoming_all.append(entry)
                if evt.get("is_all_day", 0) == 0:
                    upcoming_timed.append(entry)
        upcoming = upcoming_timed if upcoming_timed else upcoming_all
        if not upcoming:
            return None
        upcoming.sort(key=lambda x: x[0])
        return upcoming[0][1]

    def status(self) -> dict[str, Any]:
        """Return module status for health checks.

        Returns a dict with module metadata and current state.
        """
        return {
            "module": MODULE_NAME,
            "pyicloud_available": PYICLOUD_AVAILABLE,
            "credentials_configured": bool(self._apple_id and self._password),
            "db_path": self.db_path,
        }


# ---------------------------------------------------------------------------
# Standalone testing
# ---------------------------------------------------------------------------

def _create_test_db(db_path: str) -> None:
    """Create a test database with schema and sample data."""
    conn = sqlite3.connect(db_path)
    conn.executescript(_CALENDAR_EVENTS_DDL)
    # Also create the context table for full testing.
    # The prod schema uses WITHOUT ROWID with AUTOINCREMENT which SQLite rejects
    # in some versions; we use a compatible layout that preserves the UNIQUE
    # constraint on (module, key, source) which is what our UPSERT targets.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS context (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            source      TEXT    NOT NULL,
            module      TEXT    NOT NULL,
            key         TEXT    NOT NULL,
            value       TEXT    NOT NULL DEFAULT '{}',
            priority    INTEGER NOT NULL DEFAULT 0,
            expires_at  TEXT,
            created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            CONSTRAINT ctx_unique_latest UNIQUE (module, key, source)
        )
    """)
    conn.commit()
    conn.close()


def _seed_test_events(db_path: str) -> None:
    """Insert sample calendar events for testing."""
    now = _now_utc()
    conn = sqlite3.connect(db_path)

    events = [
        {
            "icloud_id": "test-001",
            "title": "Morning Standup",
            "location": "Zoom",
            "start_time": now.replace(hour=9, minute=0, second=0, microsecond=0).isoformat(),
            "end_time": now.replace(hour=9, minute=30, second=0, microsecond=0).isoformat(),
            "is_all_day": 0,
            "busy_free": "busy",
            "source": "manual",
        },
        {
            "icloud_id": "test-002",
            "title": "Deep Work Block",
            "location": "",
            "start_time": now.replace(hour=10, minute=0, second=0, microsecond=0).isoformat(),
            "end_time": now.replace(hour=12, minute=0, second=0, microsecond=0).isoformat(),
            "is_all_day": 0,
            "busy_free": "busy",
            "source": "manual",
        },
        {
            "icloud_id": "test-003",
            "title": "Lunch with Team",
            "location": "Cafe downstairs",
            "start_time": now.replace(hour=12, minute=30, second=0, microsecond=0).isoformat(),
            "end_time": now.replace(hour=13, minute=30, second=0, microsecond=0).isoformat(),
            "is_all_day": 0,
            "busy_free": "busy",
            "source": "manual",
        },
        {
            "icloud_id": "test-004",
            "title": "Company Holiday",
            "location": "",
            "start_time": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            "end_time": (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat(),
            "is_all_day": 1,
            "busy_free": "free",
            "source": "manual",
        },
        {
            "icloud_id": "test-005",
            "title": "Password Review Session",
            "location": "Security Office",
            "start_time": now.replace(hour=15, minute=0, second=0, microsecond=0).isoformat(),
            "end_time": now.replace(hour=16, minute=0, second=0, microsecond=0).isoformat(),
            "is_all_day": 0,
            "busy_free": "busy",
            "source": "manual",
        },
    ]

    for evt in events:
        conn.execute(
            _EVENT_UPSERT_SQL,
            evt,
        )

    conn.commit()
    conn.close()
    print(f"Seeded {len(events)} test events.")


if __name__ == "__main__":
    import tempfile
    import os

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    print("=" * 60)
    print("Helios v5 — Calendar Module Standalone Test")
    print("=" * 60)

    # Create a temp database for testing
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        # Set up schema and seed data
        _create_test_db(db_path)
        _seed_test_events(db_path)

        # Instantiate module with no iCloud credentials (mock/cache mode)
        config = {
            "enabled": True,
            "interval": 1800,
            # No apple_id/password — will operate in cache mode
        }
        module = CalendarModule(db_path, config)

        # --- Test status ---
        print("\n--- Module Status ---")
        status = module.status()
        for k, v in status.items():
            print(f"  {k}: {v}")

        # --- Test tick ---
        print("\n--- Running tick() ---")
        result = module.tick()
        for k, v in result.items():
            print(f"  {k}: {v}")

        # --- Test get_events_today ---
        print("\n--- Today's Events ---")
        events = module.get_events_today()
        for evt in events:
            all_day_flag = "[ALL-DAY]" if evt["is_all_day"] else ""
            start_short = evt["start_time"][:19]  # trim to seconds
            end_short = evt["end_time"][:19]
            print(
                f"  {start_short} -> {end_short}  "
                f"{evt['title']}  ({evt['busy_free']})  {all_day_flag}"
            )

        # --- Test get_free_blocks_today ---
        print("\n--- Free Blocks Today ---")
        free_blocks = module.get_free_blocks_today()
        for start, end, minutes in free_blocks:
            print(f"  {start.strftime('%H:%M')} - {end.strftime('%H:%M')}  ({minutes} min)")

        # --- Test get_next_event ---
        print("\n--- Next Event ---")
        next_evt = module.get_next_event()
        if next_evt:
            print(f"  {next_evt['title']} at {next_evt['start_time']}")
        else:
            print("  No upcoming events")

        # --- Verify context table ---
        print("\n--- Context Table ---")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT key, value, priority FROM context WHERE module = ? ORDER BY key",
            (MODULE_NAME,),
        ).fetchall()
        for row in rows:
            print(f"  {row['key']}: {row['value']}  (priority={row['priority']})")
        conn.close()

        # --- Test sanitization ---
        print("\n--- Title Sanitization ---")
        test_titles = [
            "Team Meeting",
            "Password Reset for API Key",
            "  Whitespace-padded event  ",
            "x" * 300,
        ]
        for title in test_titles:
            sanitized = _sanitize_title(title)
            display = sanitized if len(sanitized) <= 60 else sanitized[:57] + "..."
            print(f"  '{title[:40]}...' -> '{display}'")

        # --- Test graceful fallback ---
        print("\n--- Graceful Fallback Test ---")
        bad_config = {
            "enabled": True,
            "apple_id": "nonexistent@example.com",
            "password": "wrong-password",
        }
        fallback_module = CalendarModule(db_path, bad_config)
        result2 = fallback_module.tick()
        print(f"  Fallback tick result: {result2}")
        print(f"  Source: {result2['source']} (should be 'cache')")

        print("\n" + "=" * 60)
        print("All tests completed successfully!")
        print("=" * 60)

    finally:
        # Clean up temp DB
        if os.path.exists(db_path):
            os.unlink(db_path)
            print(f"\nCleaned up temp DB: {db_path}")