"""Helios — Agenda Helper.

Deterministic, read-only analysis of calendar events, tasks, and reminders.
Accepts pre-fetched data lists — no external calls, no mutation.

Functions:
    build_agenda       — merge calendar events, tasks, reminders into AgendaItems
    find_conflicts     — detect overlapping event pairs
    find_free_blocks   — find gaps ≥ 60 min between events in a day
    next_appointment   — return the nearest future AgendaItem
    overdue_items      — return items past their due time
    today_focus_list   — summary dict for a daily briefing
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class AgendaItem:
    """A single agenda entry — event, task, or reminder."""

    kind: str  # "event", "task", "reminder"
    title: str
    start: str | None  # ISO-8601 datetime or None for all-day
    due: str | None  # ISO-8601 datetime or None
    source: str  # "home_assistant", "local_cache", etc.
    priority: int  # 0=low, 1=normal, 2=high, 3=urgent
    confidence: str  # "high", "medium", "low", "unknown"

    # ---- convenience helpers ------------------------------------------------

    @property
    def start_dt(self) -> datetime | None:
        """Parse ``start`` into a timezone-aware datetime (UTC if naive)."""
        if self.start is None:
            return None
        return _parse_iso(self.start)

    @property
    def due_dt(self) -> datetime | None:
        """Parse ``due`` into a timezone-aware datetime (UTC if naive)."""
        if self.due is None:
            return None
        return _parse_iso(self.due)

    @property
    def effective_dt(self) -> datetime | None:
        """Best single timestamp for sorting: start for events, due for tasks/reminders."""
        if self.kind == "event":
            return self.start_dt
        return self.due_dt

    def __lt__(self, other: object) -> bool:
        """Sort by effective time (None last), then by priority descending."""
        if not isinstance(other, AgendaItem):
            return NotImplemented
        s = self.effective_dt
        o = other.effective_dt
        if s is None and o is None:
            return self.priority > other.priority
        if s is None:
            return False
        if o is None:
            return True
        if s != o:
            return s < o
        return self.priority > other.priority

    def __le__(self, other: object) -> bool:
        return self == other or self < other

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, AgendaItem):
            return NotImplemented
        return other < self

    def __ge__(self, other: object) -> bool:
        return self == other or self > other


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 string, treating naive datetimes as UTC."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_date(val: str | None) -> datetime | None:
    """Parse ISO string to datetime, returning None on None/empty."""
    if not val:
        return None
    return _parse_iso(val)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_agenda(
    calendar_events: Sequence[dict[str, Any]],
    tasks: Sequence[dict[str, Any]],
    reminders: Sequence[dict[str, Any]],
    today: str | None = None,
) -> list[AgendaItem]:
    """Convert raw dicts from various sources into a unified, sorted agenda.

    Each input list contains dicts that map loosely to :class:`AgendaItem` fields.
    Supported dict keys (snake_case or camelCase):

    calendar_events: title, start/start_time, end/end_time, source, priority,
                     confidence, is_all_day
    tasks:           title, due/due_time, source, priority, confidence
    reminders:       title, due/remind_time, source, priority, confidence

    If *today* is given (ISO date ``YYYY-MM-DD``), only items whose start or
    due date falls on that day are kept.
    """
    items: list[AgendaItem] = []
    today_date: datetime | None = None
    if today:
        today_date = datetime.fromisoformat(today + "T00:00:00+00:00")

    def _same_day(dt_str: str | None) -> bool:
        if today_date is None:
            return True
        if not dt_str:
            return False
        dt = _parse_iso(dt_str)
        return dt.date() == today_date.date()

    # --- calendar events ---
    for ev in calendar_events:
        start_val = ev.get("start_time") or ev.get("start")
        if not _same_day(start_val):
            continue
        items.append(AgendaItem(
            kind="event",
            title=ev.get("title", "(untitled event)"),
            start=start_val,
            due=ev.get("end_time") or ev.get("end"),
            source=ev.get("source", "unknown"),
            priority=int(ev.get("priority", 1)),
            confidence=ev.get("confidence", "unknown"),
        ))

    # --- tasks ---
    for t in tasks:
        due_val = t.get("due_time") or t.get("due")
        if not _same_day(due_val):
            continue
        items.append(AgendaItem(
            kind="task",
            title=t.get("title", "(untitled task)"),
            start=None,
            due=due_val,
            source=t.get("source", "unknown"),
            priority=int(t.get("priority", 1)),
            confidence=t.get("confidence", "unknown"),
        ))

    # --- reminders ---
    for r in reminders:
        due_val = r.get("remind_time") or r.get("due")
        if not _same_day(due_val):
            continue
        items.append(AgendaItem(
            kind="reminder",
            title=r.get("title", "(untitled reminder)"),
            start=None,
            due=due_val,
            source=r.get("source", "unknown"),
            priority=int(r.get("priority", 1)),
            confidence=r.get("confidence", "unknown"),
        ))

    items.sort()
    return items


def find_conflicts(items: list[AgendaItem]) -> list[tuple[AgendaItem, AgendaItem]]:
    """Return pairs of events that overlap in time.

    Only items with ``kind == "event"`` and a non-None ``start`` are
    considered.  An event's end is its ``due`` (end_time); if missing, the
    event is assumed to last 60 minutes.
    """
    events = [i for i in items if i.kind == "event" and i.start is not None]
    events.sort(key=lambda e: e.start_dt)  # type: ignore[arg-type]

    conflicts: list[tuple[AgendaItem, AgendaItem]] = []
    for i in range(len(events)):
        a = events[i]
        a_start = a.start_dt
        assert a_start is not None  # guaranteed by filter above
        a_end = _to_date(a.due) or (a_start + timedelta(minutes=60))
        for j in range(i + 1, len(events)):
            b = events[j]
            b_start = b.start_dt
            assert b_start is not None
            b_end = _to_date(b.due) or (b_start + timedelta(minutes=60))
            if b_start < a_end and a_start < b_end:
                conflicts.append((a, b))
            else:
                # events are sorted by start, so once b starts at or after
                # a ends, no further b can overlap a
                break
    return conflicts


def find_free_blocks(
    items: list[AgendaItem],
    day_start: str = "07:00",
    day_end: str = "23:00",
) -> list[tuple[str, str, int]]:
    """Find free time blocks of 60+ minutes between events.

    Returns a list of ``(start_iso, end_iso, duration_min)`` tuples.

    Only events with a ``start`` time are considered; tasks/reminders are
    ignored since they don't occupy calendar time.
    """
    events = [i for i in items if i.kind == "event" and i.start is not None]
    events.sort(key=lambda e: e.start_dt)  # type: ignore[arg-type]

    # Determine the reference date from the first event, or fall back to today
    ref_date = datetime.now(timezone.utc).date()
    if events and events[0].start_dt is not None:
        ref_date = events[0].start_dt.date()  # type: ignore[union-attr]

    day_start_dt = datetime.combine(
        ref_date,
        datetime.strptime(day_start, "%H:%M").time(),
        tzinfo=timezone.utc,
    )
    day_end_dt = datetime.combine(
        ref_date,
        datetime.strptime(day_end, "%H:%M").time(),
        tzinfo=timezone.utc,
    )

    cursor = day_start_dt
    blocks: list[tuple[str, str, int]] = []

    for ev in events:
        ev_start = ev.start_dt
        assert ev_start is not None  # guaranteed by filter
        ev_end = _to_date(ev.due) or (ev_start + timedelta(minutes=60))

        # Skip events outside the day window
        if ev_end <= cursor:
            continue
        if ev_start >= day_end_dt:
            break

        clip_start = max(ev_start, day_start_dt)
        clip_end = min(ev_end, day_end_dt)

        # Clamp event to the day boundary
        if clip_start < cursor:
            # Event starts before cursor — advance cursor past the event
            cursor = max(cursor, clip_end)
            continue

        gap_minutes = int((clip_start - cursor).total_seconds() / 60)
        if gap_minutes >= 60:
            blocks.append((
                cursor.isoformat(),
                clip_start.isoformat(),
                gap_minutes,
            ))
        cursor = max(cursor, clip_end)

    # Remaining time after last event
    if cursor < day_end_dt:
        gap_minutes = int((day_end_dt - cursor).total_seconds() / 60)
        if gap_minutes >= 60:
            blocks.append((
                cursor.isoformat(),
                day_end_dt.isoformat(),
                gap_minutes,
            ))

    return blocks


def next_appointment(
    items: list[AgendaItem],
    now: datetime | None = None,
) -> AgendaItem | None:
    """Return the nearest future event relative to *now*.

    Only items with ``kind == "event"`` and a non-None ``start`` are
    considered.  Returns ``None`` if no future event exists.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    candidates = [
        i for i in items
        if i.kind == "event" and i.start_dt is not None and i.start_dt > now
    ]
    if not candidates:
        return None
    candidates.sort()
    return candidates[0]


def overdue_items(
    items: list[AgendaItem],
    now: datetime | None = None,
) -> list[AgendaItem]:
    """Return tasks/reminders whose ``due`` time is past *now*.

    Only items with ``kind`` in ``("task", "reminder")`` and a non-None
    ``due`` are considered.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    result = [
        i for i in items
        if i.kind in ("task", "reminder")
        and i.due_dt is not None
        and i.due_dt <= now
    ]
    result.sort()
    return result


def today_focus_list(items: list[AgendaItem]) -> dict[str, Any]:
    """Produce a summary dict for a daily briefing.

    Returns::

        {
            "events_count": int,
            "next_event": AgendaItem | None,
            "free_minutes": int,
            "overdue_count": int,
        }
    """
    events = [i for i in items if i.kind == "event"]
    free_blocks = find_free_blocks(items)
    total_free = sum(b[2] for b in free_blocks)

    now = datetime.now(timezone.utc)
    nxt = next_appointment(items, now=now)
    overdue = overdue_items(items, now=now)

    return {
        "events_count": len(events),
        "next_event": nxt,
        "free_minutes": total_free,
        "overdue_count": len(overdue),
    }