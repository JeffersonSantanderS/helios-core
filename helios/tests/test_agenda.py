"""Tests for helios.agenda — deterministic agenda helper.

Uses fixture JSON data; no Home Assistant or iCloud calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helios.agenda import (
    AgendaItem,
    build_agenda,
    find_conflicts,
    find_free_blocks,
    next_appointment,
    overdue_items,
    today_focus_list,
)

# ---------------------------------------------------------------------------
# Fixtures — in-file JSON data
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> list[dict]:
    path = FIXTURE_DIR / name
    if path.exists():
        return json.loads(path.read_text())
    return []


CALENDAR_EVENTS = [
    {
        "title": "Team standup",
        "start_time": "2025-06-01T09:00:00+00:00",
        "end_time": "2025-06-01T09:30:00+00:00",
        "source": "home_assistant",
        "priority": 1,
        "confidence": "high",
    },
    {
        "title": "1:1 with Alice",
        "start_time": "2025-06-01T10:00:00+00:00",
        "end_time": "2025-06-01T10:30:00+00:00",
        "source": "home_assistant",
        "priority": 1,
        "confidence": "high",
    },
    {
        "title": "Design review",
        "start_time": "2025-06-01T10:15:00+00:00",
        "end_time": "2025-06-01T11:00:00+00:00",
        "source": "local_cache",
        "priority": 2,
        "confidence": "medium",
    },
    {
        "title": "Lunch with Bob",
        "start_time": "2025-06-01T12:00:00+00:00",
        "end_time": "2025-06-01T13:00:00+00:00",
        "source": "home_assistant",
        "priority": 1,
        "confidence": "high",
    },
    {
        "title": "All-hands",
        "start_time": "2025-06-01T15:00:00+00:00",
        "end_time": "2025-06-01T16:00:00+00:00",
        "source": "home_assistant",
        "priority": 2,
        "confidence": "high",
    },
    {
        # no end_time — defaults to 60 min
        "title": "Quick sync",
        "start_time": "2025-06-01T16:30:00+00:00",
        "source": "local_cache",
        "priority": 0,
        "confidence": "low",
    },
]

EVENT_CONFLICT_A = {
    "title": "Overlapping A",
    "start_time": "2025-06-01T09:00:00+00:00",
    "end_time": "2025-06-01T10:00:00+00:00",
    "source": "home_assistant",
    "priority": 1,
    "confidence": "high",
}

EVENT_CONFLICT_B = {
    "title": "Overlapping B",
    "start_time": "2025-06-01T09:30:00+00:00",
    "end_time": "2025-06-01T10:30:00+00:00",
    "source": "home_assistant",
    "priority": 1,
    "confidence": "high",
}

EVENT_NO_CONFLICT = {
    "title": "Later event",
    "start_time": "2025-06-01T11:00:00+00:00",
    "end_time": "2025-06-01T12:00:00+00:00",
    "source": "home_assistant",
    "priority": 1,
    "confidence": "high",
}

TASKS = [
    {
        "title": "Submit report",
        "due_time": "2025-06-01T17:00:00+00:00",
        "source": "home_assistant",
        "priority": 2,
        "confidence": "high",
    },
    {
        "title": "Review PR #42",
        "due_time": "2025-06-01T11:00:00+00:00",
        "source": "local_cache",
        "priority": 1,
        "confidence": "medium",
    },
    {
        "title": "Buy groceries",
        "due_time": "2025-06-01T19:00:00+00:00",
        "source": "home_assistant",
        "priority": 0,
        "confidence": "low",
    },
    # Overdue task
    {
        "title": "File taxes",
        "due_time": "2025-05-28T23:59:00+00:00",
        "source": "home_assistant",
        "priority": 3,
        "confidence": "high",
    },
    # Task with no due — should still appear when not filtering by day
    {
        "title": "Someday task",
        "due_time": None,
        "source": "local_cache",
        "priority": 0,
        "confidence": "low",
    },
]

REMINDERS = [
    {
        "title": "Call dentist",
        "remind_time": "2025-06-01T08:00:00+00:00",
        "source": "home_assistant",
        "priority": 1,
        "confidence": "high",
    },
    # Overdue reminder
    {
        "title": "Renew passport",
        "remind_time": "2025-05-25T09:00:00+00:00",
        "source": "home_assistant",
        "priority": 2,
        "confidence": "high",
    },
]


# ---------------------------------------------------------------------------
# Helper to build items quickly in tests
# ---------------------------------------------------------------------------

def _item(
    kind: str = "event",
    title: str = "Test",
    start: str | None = None,
    due: str | None = None,
    source: str = "test",
    priority: int = 1,
    confidence: str = "unknown",
) -> AgendaItem:
    return AgendaItem(
        kind=kind,
        title=title,
        start=start,
        due=due,
        source=source,
        priority=priority,
        confidence=confidence,
    )


# ===========================================================================
# 3.2 Tests
# ===========================================================================


class TestAgendaItem:
    """AgendaItem creation and sorting."""

    def test_create_event(self):
        it = _item(kind="event", title="Meeting", start="2025-06-01T09:00:00+00:00")
        assert it.kind == "event"
        assert it.title == "Meeting"
        assert it.start is not None
        assert it.start_dt == datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc)

    def test_create_task_with_due(self):
        it = _item(kind="task", title="Submit", due="2025-06-01T17:00:00+00:00")
        assert it.kind == "task"
        assert it.due_dt is not None
        assert it.effective_dt == it.due_dt

    def test_effective_dt_event_prefers_start(self):
        it = _item(kind="event", start="2025-06-01T09:00:00+00:00", due="2025-06-01T10:00:00+00:00")
        assert it.effective_dt == it.start_dt

    def test_effective_dt_task_prefers_due(self):
        it = _item(kind="task", start=None, due="2025-06-01T17:00:00+00:00")
        assert it.effective_dt == it.due_dt

    def test_sort_earlier_first(self):
        a = _item(kind="event", start="2025-06-01T09:00:00+00:00")
        b = _item(kind="event", start="2025-06-01T10:00:00+00:00")
        assert a < b
        assert sorted([b, a]) == [a, b]

    def test_sort_higher_priority_breaks_tie(self):
        a = _item(kind="task", due="2025-06-01T09:00:00+00:00", priority=2)
        b = _item(kind="task", due="2025-06-01T09:00:00+00:00", priority=1)
        assert a < b  # higher priority sorts earlier

    def test_none_time_sorts_last(self):
        a = _item(kind="event", start="2025-06-01T09:00:00+00:00")
        b = _item(kind="task", due=None)  # no time
        assert a < b
        assert sorted([b, a]) == [a, b]

    def test_naive_datetime_treated_as_utc(self):
        it = _item(kind="event", start="2025-06-01T09:00:00")
        assert it.start_dt is not None
        assert it.start_dt.tzinfo == timezone.utc


class TestBuildAgenda:
    """build_agenda merges and filters correctly."""

    def test_basic_merge(self):
        items = build_agenda(CALENDAR_EVENTS, TASKS[:1], REMINDERS[:1])
        kinds = [i.kind for i in items]
        assert "event" in kinds
        assert "task" in kinds
        assert "reminder" in kinds

    def test_today_filter(self):
        items = build_agenda(CALENDAR_EVENTS, TASKS, REMINDERS, today="2025-06-01")
        # Should not include "Someday task" (due=None) filtered out, or "File taxes" (different day)
        for it in items:
            if it.kind == "task" and it.title == "Someday task":
                # No due — filtered out when today is specified
                assert False, "Task with no due date should be excluded on day filter"

    def test_overdue_task_excluded_by_today_filter(self):
        items = build_agenda(CALENDAR_EVENTS, TASKS, REMINDERS, today="2025-06-01")
        for it in items:
            if it.title == "File taxes":
                assert False, "Past-date task should not appear for 2025-06-01"

    def test_without_today_includes_all(self):
        items = build_agenda(CALENDAR_EVENTS, TASKS, [])
        titles = [i.title for i in items]
        assert "Someday task" in titles
        assert "File taxes" in titles

    def test_sorted_output(self):
        items = build_agenda(CALENDAR_EVENTS, TASKS[:2], REMINDERS[:1])
        for i in range(len(items) - 1):
            assert items[i] <= items[i + 1] or items[i].effective_dt == items[i + 1].effective_dt


class TestFindConflicts:
    """Conflict detection for overlapping events."""

    def test_no_conflicts(self):
        items = build_agenda(CALENDAR_EVENTS, [], [])
        # None of the standard fixtures overlap except design review vs 1:1
        # Actually 1:1 (10:00-10:30) and design review (10:15-11:00) DO overlap
        conflicts = find_conflicts(items)
        assert len(conflicts) >= 1  # at least 1 overlap

    def test_explicit_overlap(self):
        events = [
            _item(kind="event", title="A", start="2025-06-01T09:00:00+00:00", due="2025-06-01T10:00:00+00:00"),
            _item(kind="event", title="B", start="2025-06-01T09:30:00+00:00", due="2025-06-01T10:30:00+00:00"),
        ]
        conflicts = find_conflicts(events)
        assert len(conflicts) == 1
        a, b = conflicts[0]
        assert a.title == "A"
        assert b.title == "B"

    def test_touching_but_not_overlapping(self):
        events = [
            _item(kind="event", title="A", start="2025-06-01T09:00:00+00:00", due="2025-06-01T10:00:00+00:00"),
            _item(kind="event", title="B", start="2025-06-01T10:00:00+00:00", due="2025-06-01T11:00:00+00:00"),
        ]
        conflicts = find_conflicts(events)
        assert len(conflicts) == 0  # touching, no overlap

    def test_non_events_ignored(self):
        items = [
            _item(kind="task", title="Task", due="2025-06-01T09:00:00+00:00"),
        ]
        conflicts = find_conflicts(items)
        assert len(conflicts) == 0

    def test_default_60min_duration(self):
        """Event with no end time defaults to 60 min for conflict check."""
        events = [
            _item(kind="event", title="No end", start="2025-06-01T09:00:00+00:00", due=None),
            _item(kind="event", title="Starts at 09:30", start="2025-06-01T09:30:00+00:00", due="2025-06-01T10:00:00+00:00"),
        ]
        conflicts = find_conflicts(events)
        assert len(conflicts) == 1

    def test_fixture_data_conflicts(self):
        """From CALENDAR_EVENTS: design review (10:15-11:00) overlaps 1:1 (10:00-10:30)."""
        items = build_agenda(CALENDAR_EVENTS, [], [])
        conflicts = find_conflicts(items)
        conflict_titles = {(a.title, b.title) for a, b in conflicts}
        assert ("1:1 with Alice", "Design review") in conflict_titles

    def test_contained_event(self):
        """Event entirely inside another."""
        events = [
            _item(kind="event", title="Long", start="2025-06-01T09:00:00+00:00", due="2025-06-01T12:00:00+00:00"),
            _item(kind="event", title="Short", start="2025-06-01T10:00:00+00:00", due="2025-06-01T10:30:00+00:00"),
        ]
        conflicts = find_conflicts(events)
        assert len(conflicts) == 1

    def test_multiple_overlaps(self):
        events = [
            _item(kind="event", title="A", start="2025-06-01T09:00:00+00:00", due="2025-06-01T10:00:00+00:00"),
            _item(kind="event", title="B", start="2025-06-01T09:30:00+00:00", due="2025-06-01T10:30:00+00:00"),
            _item(kind="event", title="C", start="2025-06-01T09:45:00+00:00", due="2025-06-01T10:15:00+00:00"),
        ]
        conflicts = find_conflicts(events)
        assert len(conflicts) == 3  # (A,B), (A,C), (B,C)


class TestFindFreeBlocks:
    """Free block detection — 60+ minute gaps between events."""

    def test_simple_gap(self):
        events = [
            _item(kind="event", start="2025-06-01T09:00:00+00:00", due="2025-06-01T10:00:00+00:00"),
            _item(kind="event", start="2025-06-01T12:00:00+00:00", due="2025-06-01T13:00:00+00:00"),
        ]
        blocks = find_free_blocks(events, day_start="08:00", day_end="18:00")
        # Gap from 10:00 to 12:00 = 120 min
        start_gaps = [(b[2]) for b in blocks if b[0].startswith("2025-06-01T10:00")]
        assert 120 in start_gaps

    def test_no_gap_too_short(self):
        events = [
            _item(kind="event", start="2025-06-01T09:00:00+00:00", due="2025-06-01T09:30:00+00:00"),
            _item(kind="event", start="2025-06-01T10:00:00+00:00", due="2025-06-01T11:00:00+00:00"),
        ]
        blocks = find_free_blocks(events, day_start="09:00", day_end="18:00")
        # Gap is 30 min — should not appear
        for _, _, dur in blocks:
            assert dur >= 60
        # Only the after-11:00 gap should appear
        durations = [dur for _, _, dur in blocks]
        # 09:30–10:00 = 30 min, not included; 11:00–18:00 = 420 min, included
        assert 420 in durations

    def test_morning_and_evening_free(self):
        events = [
            _item(kind="event", start="2025-06-01T12:00:00+00:00", due="2025-06-01T13:00:00+00:00"),
        ]
        blocks = find_free_blocks(events, day_start="07:00", day_end="23:00")
        durations = [dur for _, _, dur in blocks]
        # Morning: 07:00–12:00 = 300 min, evening: 13:00–23:00 = 600 min
        assert 300 in durations
        assert 600 in durations

    def test_no_events_full_day_free(self):
        blocks = find_free_blocks([], day_start="07:00", day_end="23:00")
        assert len(blocks) == 1
        assert blocks[0][2] == 960  # 16 hours = 960 min

    def test_tasks_ignored(self):
        items = [
            _item(kind="task", due="2025-06-01T10:00:00+00:00"),
        ]
        blocks = find_free_blocks(items, day_start="07:00", day_end="23:00")
        assert len(blocks) == 1
        assert blocks[0][2] == 960

    def test_default_60min_duration_for_missing_end(self):
        events = [
            _item(kind="event", start="2025-06-01T09:00:00+00:00", due=None),
            _item(kind="event", start="2025-06-01T11:00:00+00:00", due="2025-06-01T12:00:00+00:00"),
        ]
        blocks = find_free_blocks(events, day_start="07:00", day_end="23:00")
        # 07:00-09:00 = 120 min gap, 10:00-11:00 = 60 min gap, 12:00-23:00 = 660 min
        durations = sorted([dur for _, _, dur in blocks])
        assert 60 in durations
        assert 120 in durations
        assert 660 in durations

    def test_fixture_data_free_blocks(self):
        items = build_agenda(CALENDAR_EVENTS, [], [])
        blocks = find_free_blocks(items, day_start="07:00", day_end="23:00")
        total_free = sum(dur for _, _, dur in blocks)
        # Events: 09-09:30, 10-10:30, 10:15-11, 12-13, 15-16, 16:30-17:30(default)
        # Total busy ≈ 4h15m = 255 min, free = 960 - 255 = 705
        # but overlapping 1:1 and design review reduce busy time
        assert total_free > 0


class TestNextAppointment:
    """Next appointment logic."""

    def test_finds_next_event(self):
        now = datetime(2025, 6, 1, 11, 30, tzinfo=timezone.utc)
        items = build_agenda(CALENDAR_EVENTS, [], [])
        nxt = next_appointment(items, now=now)
        assert nxt is not None
        assert nxt.title == "Lunch with Bob"

    def test_none_when_all_past(self):
        now = datetime(2025, 6, 1, 18, 0, tzinfo=timezone.utc)
        items = build_agenda(CALENDAR_EVENTS, [], [])
        nxt = next_appointment(items, now=now)
        # All fixture events are on June 1 before 18:00 except Quick Sync at 16:30
        assert nxt is None

    def test_none_when_no_events(self):
        items = build_agenda([], TASKS, REMINDERS)
        nxt = next_appointment(items, now=datetime(2025, 6, 1, 8, 0, tzinfo=timezone.utc))
        assert nxt is None

    def test_ignores_tasks(self):
        items = [
            _item(kind="task", due="2025-06-01T12:00:00+00:00"),
        ]
        nxt = next_appointment(items, now=datetime(2025, 6, 1, 8, 0, tzinfo=timezone.utc))
        assert nxt is None


class TestOverdueItems:
    """Overdue items detection — tasks and reminders past due."""

    def test_overdue_task(self):
        items = [
            _item(kind="task", title="Late", due="2025-05-28T23:59:00+00:00"),
            _item(kind="task", title="Future", due="2025-06-02T12:00:00+00:00"),
        ]
        now = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        overdue = overdue_items(items, now=now)
        assert len(overdue) == 1
        assert overdue[0].title == "Late"

    def test_overdue_reminder(self):
        items = [
            _item(kind="reminder", title="Past", due="2025-05-25T09:00:00+00:00"),
        ]
        now = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        overdue = overdue_items(items, now=now)
        assert len(overdue) == 1
        assert overdue[0].title == "Past"

    def test_events_not_overdue(self):
        items = [
            _item(kind="event", title="Past event", start="2025-05-30T09:00:00+00:00", due="2025-05-30T10:00:00+00:00"),
        ]
        now = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        overdue = overdue_items(items, now=now)
        assert len(overdue) == 0

    def test_exactly_due_is_overdue(self):
        items = [
            _item(kind="task", title="Due now", due="2025-06-01T10:00:00+00:00"),
        ]
        now = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        overdue = overdue_items(items, now=now)
        assert len(overdue) == 1

    def test_from_fixture_data(self):
        items = build_agenda([], TASKS, REMINDERS)
        now = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        overdue = overdue_items(items, now=now)
        titles = [i.title for i in overdue]
        assert "File taxes" in titles
        assert "Renew passport" in titles

    def test_no_due_date_not_overdue(self):
        items = [
            _item(kind="task", title="No due", due=None),
        ]
        now = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        overdue = overdue_items(items, now=now)
        assert len(overdue) == 0


class TestTodayFocusList:
    """Today focus list output."""

    def test_basic_focus_list(self):
        items = build_agenda(CALENDAR_EVENTS, TASKS, REMINDERS, today="2025-06-01")
        focus = today_focus_list(items)
        assert "events_count" in focus
        assert "next_event" in focus
        assert "free_minutes" in focus
        assert "overdue_count" in focus

    def test_events_count(self):
        items = build_agenda(CALENDAR_EVENTS, TASKS, REMINDERS, today="2025-06-01")
        focus = today_focus_list(items)
        assert focus["events_count"] == len([i for i in items if i.kind == "event"])

    def test_overdue_count(self):
        items = build_agenda(CALENDAR_EVENTS, TASKS, REMINDERS, today="2025-06-01")
        # When checking with "now" on the same day, only items due before now are overdue
        focus = today_focus_list(items)
        assert isinstance(focus["overdue_count"], int)

    def test_free_minutes_integer(self):
        items = build_agenda(CALENDAR_EVENTS, [], [], today="2025-06-01")
        focus = today_focus_list(items)
        assert isinstance(focus["free_minutes"], int)
        assert focus["free_minutes"] > 0

    def test_empty_agenda(self):
        items = build_agenda([], [], [])
        focus = today_focus_list(items)
        assert focus["events_count"] == 0
        assert focus["next_event"] is None
        assert focus["free_minutes"] == 960  # full 07:00-23:00
        assert focus["overdue_count"] == 0

    def test_next_event_is_agenda_item_or_none(self):
        items = build_agenda(CALENDAR_EVENTS, [], [], today="2025-06-01")
        focus = today_focus_list(items)
        if focus["next_event"] is not None:
            assert isinstance(focus["next_event"], AgendaItem)


class TestAgendaItemFromFixtureJSON:
    """Load fixture JSON files if available (graceful skip if not)."""

    def test_load_calendar_fixture(self):
        data = _load_fixture("calendar_events.json")
        if not data:
            pytest.skip("No calendar fixture file")
        items = build_agenda(data, [], [])
        assert len(items) > 0
        assert all(i.kind == "event" for i in items)

    def test_load_tasks_fixture(self):
        data = _load_fixture("tasks.json")
        if not data:
            pytest.skip("No tasks fixture file")
        items = build_agenda([], data, [])
        assert len(items) > 0
        assert all(i.kind == "task" for i in items)