"""Helios v7 — Spotify session builder and daily summary report.

Builds listening-session data from spotify_history.jsonl, then produces
a daily summary report using the report.v1 schema helpers.

Key design decisions:
- Sessions are groups of consecutive entries where the gap between entries
  is < 5 minutes. Both plays and skips participate in session grouping so
  that a skip between two plays doesn't artificially split the session.
- Short plays (< 30 seconds) are "skips" — they count in skipped_count but
  do NOT contribute to total_minutes or track_count.
- Duplicate polls (same track+artist within 60 seconds of a previous
  entry) are deduplicated so they don't inflate listen time.
- Late-night plays are those between midnight and 4 AM local time
  (America/Edmonton).
- The builder is entirely read-only: it never mutates the input playlist.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import Any, Optional

from . import REPORT_SCHEMA_VERSION, write_report_json, write_report_markdown

logger = logging.getLogger("helios.reports.spotify")

# ── Constants ────────────────────────────────────────────────────────────

# Gap between consecutive entries exceeding this => new session
SESSION_GAP_SECONDS = 300  # 5 minutes

# Plays shorter than this are "skips"
SKIP_THRESHOLD_MS = 30_000  # 30 seconds

# Duplicate-poll dedup window: same track+artist within this many seconds
# of a previous entry counts as a duplicate poll, not a new play.
DEDUP_WINDOW_SECONDS = 60

# Late-night window (local local time)
LATE_NIGHT_START_HOUR = 0  # midnight
LATE_NIGHT_END_HOUR = 4    # 4 AM

# Timezone for late-night detection
ALBERTA_TZ = "America/Edmonton"

# ── Data classes ─────────────────────────────────────────────────────────


class SpotifySession:
    """One listening session: a contiguous block of entries."""

    __slots__ = (
        "start", "end", "total_minutes", "track_count",
        "skipped_count", "dominant_artists", "late_night",
    )

    def __init__(
        self,
        start: datetime,
        end: datetime,
        total_minutes: float,
        track_count: int,
        skipped_count: int,
        dominant_artists: list[str],
        late_night: bool,
    ) -> None:
        self.start = start
        self.end = end
        self.total_minutes = total_minutes
        self.track_count = track_count
        self.skipped_count = skipped_count
        self.dominant_artists = dominant_artists
        self.late_night = late_night

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "total_minutes": round(self.total_minutes, 2),
            "track_count": self.track_count,
            "skipped_count": self.skipped_count,
            "dominant_artists": self.dominant_artists,
            "late_night": self.late_night,
        }


# ── Helpers ──────────────────────────────────────────────────────────────


def _parse_ts(ts_str: str) -> datetime:
    """Parse an ISO timestamp string into a timezone-aware datetime (UTC)."""
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_late_night_local(dt: datetime) -> bool:
    """Return True if *dt* falls between midnight and 4 AM local time."""
    try:
        from zoneinfo import ZoneInfo
        alberta = ZoneInfo(ALBERTA_TZ)
    except (ImportError, Exception):
        # Fallback: UTC-7 approximate
        alberta = timezone(timedelta(hours=-7))
    local_dt = dt.astimezone(alberta)
    return LATE_NIGHT_START_HOUR <= local_dt.hour < LATE_NIGHT_END_HOUR


def _is_skip(entry: dict[str, Any]) -> bool:
    """Return True if this entry is a skip (short play or paused)."""
    if entry.get("is_playing") is False:
        return True
    if (entry.get("progress_ms") or 0) < SKIP_THRESHOLD_MS:
        return True
    return False


def _get_alberta_tz():
    """Return the local timezone, with a fallback."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(ALBERTA_TZ)
    except (ImportError, Exception):
        return timezone(timedelta(hours=-7))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a .jsonl file and return a list of parsed dicts.

    Blank lines are silently skipped.
    """
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append(json.loads(line))
    return entries


# ── SpotifySessionBuilder ────────────────────────────────────────────────


class SpotifySessionBuilder:
    """Build listening sessions from spotify_history.jsonl entries.

    This is a pure read-only analysis: the input list is never mutated.

    Parameters
    ----------
    entries:
        List of dicts parsed from spotify_history.jsonl.  Each dict
        should have keys: ``ts``, ``track``, ``artist``, ``album``,
        ``is_playing``, ``duration_ms``, ``progress_ms``.
    """

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        # Take a shallow copy so the original list is never mutated.
        self._raw = list(entries)
        self._sessions: list[SpotifySession] | None = None

    # ── Public API ────────────────────────────────────────────────────

    def build_sessions(self) -> list[SpotifySession]:
        """Deduplicate, classify, and group entries into sessions.

        Returns a list of :class:`SpotifySession` objects.  The result
        is cached; subsequent calls return the same list.
        """
        if self._sessions is not None:
            return self._sessions

        deduped = self._deduplicate(self._raw)

        # Sort all deduped entries by timestamp for grouping
        deduped.sort(key=lambda e: _parse_ts(e["ts"]))

        # Group ALL entries (plays + skips) by timestamp gaps to form
        # sessions. Skips participate in grouping so they don't split
        # sessions, but they don't count toward play metrics.
        sessions = self._group_into_sessions(deduped)
        self._sessions = sessions
        return sessions

    def sessions_for_date(self, target_date: date | str) -> list[SpotifySession]:
        """Return sessions that fall on *target_date* (local local date)."""
        if isinstance(target_date, str):
            target_date = date.fromisoformat(target_date)
        sessions = self.build_sessions()
        alberta = _get_alberta_tz()
        return [s for s in sessions if s.start.astimezone(alberta).date() == target_date]

    # ── Deduplication ─────────────────────────────────────────────────

    @staticmethod
    def _deduplicate(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove duplicate polls: same track+artist within 60 s of a
        previous entry for the same track+artist.

        The first occurrence is kept; subsequent polls within the window
        are dropped.  The input list is not mutated.
        """
        if not entries:
            return []
        # Sort by timestamp so we can do a sequential scan
        sorted_entries = sorted(entries, key=lambda e: e.get("ts", ""))
        result: list[dict[str, Any]] = []
        # Track last-seen timestamp per (track, artist)
        last_seen: dict[tuple[str, str], datetime] = {}

        for entry in sorted_entries:
            key = (entry.get("track", ""), entry.get("artist", ""))
            ts = _parse_ts(entry["ts"])
            if key in last_seen:
                gap = (ts - last_seen[key]).total_seconds()
                if gap < DEDUP_WINDOW_SECONDS:
                    # Duplicate poll — skip
                    continue
            last_seen[key] = ts
            result.append(entry)

        return result

    # ── Session grouping ──────────────────────────────────────────────

    def _group_into_sessions(
        self,
        entries: list[dict[str, Any]],
    ) -> list[SpotifySession]:
        """Group consecutive entries where gap < 5 min into sessions.

        All entries (plays and skips) participate in grouping so that
        a skip between two plays doesn't break the session. Only non-skip
        entries contribute to track_count and total_minutes.
        """
        if not entries:
            return []

        sessions: list[SpotifySession] = []
        group: list[dict[str, Any]] = [entries[0]]

        for i in range(1, len(entries)):
            prev_ts = _parse_ts(entries[i - 1]["ts"])
            curr_ts = _parse_ts(entries[i]["ts"])
            gap = (curr_ts - prev_ts).total_seconds()

            if gap < SESSION_GAP_SECONDS:
                group.append(entries[i])
            else:
                sessions.append(self._make_session(group))
                group = [entries[i]]

        # Close last group
        if group:
            sessions.append(self._make_session(group))

        # Filter out sessions with zero actual plays (all skips)
        sessions = [s for s in sessions if s.track_count > 0]

        return sessions

    def _make_session(
        self,
        entries: list[dict[str, Any]],
    ) -> SpotifySession:
        """Build a SpotifySession from a group of entries (plays + skips).

        Only non-skip entries contribute to track_count, total_minutes,
        and dominant_artists.
        """
        # Separate plays from skips within this group
        plays = [e for e in entries if not _is_skip(e)]
        skips = [e for e in entries if _is_skip(e)]

        # Session time span covers ALL entries (plays + skips)
        all_timestamps = [_parse_ts(e["ts"]) for e in entries]
        start = min(all_timestamps)

        # End time: the latest timestamp plus its progress
        last_entry = max(entries, key=lambda e: _parse_ts(e["ts"]))
        end_ts = _parse_ts(last_entry["ts"])
        progress_s = (last_entry.get("progress_ms") or 0) / 1000.0
        end = max(end_ts, end_ts + timedelta(seconds=progress_s))

        # Total minutes: sum of progress from actual plays only
        total_seconds = sum((e.get("progress_ms") or 0) for e in plays) / 1000.0
        total_minutes = total_seconds / 60.0

        # Dominant artists from plays only
        artist_counter: Counter[str] = Counter()
        for p in plays:
            artist = p.get("artist", "Unknown")
            artist_counter[artist] += 1
        dominant = [a for a, _ in artist_counter.most_common(3)]

        # Late-night: any play (not skip) in the late-night window
        late_night = any(_is_late_night_local(_parse_ts(p["ts"])) for p in plays) if plays else False

        return SpotifySession(
            start=start,
            end=end,
            total_minutes=total_minutes,
            track_count=len(plays),
            skipped_count=len(skips),
            dominant_artists=dominant,
            late_night=late_night,
        )


# ── SpotifyDailySummary ──────────────────────────────────────────────────


class SpotifyDailySummary:
    """Generate a daily Spotify summary report using report.v1 schema.

    Parameters
    ----------
    entries:
        Raw spotify_history.jsonl entries (list of dicts).
    report_date:
        The date this report covers.  Defaults to today (local time).
    """

    def __init__(
        self,
        entries: list[dict[str, Any]],
        report_date: date | str | None = None,
    ) -> None:
        self._entries = list(entries)  # copy, never mutate caller's data
        if report_date is None:
            alberta = _get_alberta_tz()
            self.report_date = datetime.now(alberta).date()
        elif isinstance(report_date, str):
            self.report_date = date.fromisoformat(report_date)
        else:
            self.report_date = report_date

        self._builder = SpotifySessionBuilder(self._entries)

    # ── Build report dict ─────────────────────────────────────────────

    def build(self) -> dict[str, Any]:
        """Return the full report dict matching the report.v1 schema."""
        sessions = self._builder.sessions_for_date(self.report_date)

        total_minutes = sum(s.total_minutes for s in sessions)
        session_count = len(sessions)
        total_skipped = sum(s.skipped_count for s in sessions)
        late_night_minutes = sum(
            s.total_minutes for s in sessions if s.late_night
        )

        # Count plays on this date for confidence calculation
        deduped = self._builder._deduplicate(self._entries)
        alberta = _get_alberta_tz()

        artist_counter: Counter[str] = Counter()
        plays_on_date = 0
        for e in deduped:
            e_ts = _parse_ts(e["ts"])
            if e_ts.astimezone(alberta).date() == self.report_date and not _is_skip(e):
                artist_counter[e.get("artist", "Unknown")] += 1
                plays_on_date += 1

        top_artists = [a for a, _ in artist_counter.most_common(5)]

        # Freshness: how recent is the last play on this date?
        freshness = "stale"
        if sessions:
            last_end = max(s.end for s in sessions)
            now = datetime.now(timezone.utc)
            hours_ago = (now - last_end).total_seconds() / 3600
            if hours_ago < 1:
                freshness = "fresh"
            elif hours_ago < 6:
                freshness = "recent"
            else:
                freshness = "same_day"

        # Confidence
        confidence = self._determine_confidence(plays_on_date)

        date_str = self.report_date.isoformat()

        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_type": "spotify",
            "period_start": date_str,
            "period_end": date_str,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "confidence": confidence,
            "freshness": freshness,
            "summary": self._build_narrative(
                sessions, total_minutes, session_count, top_artists
            ),
            "items": [
                {
                    "key": "top_artists",
                    "value": top_artists,
                    "unit": "list",
                    "confidence": "observed" if top_artists else "missing",
                },
                {
                    "key": "total_minutes",
                    "value": round(total_minutes, 2),
                    "unit": "minutes",
                    "confidence": "observed" if total_minutes > 0 else "missing",
                },
                {
                    "key": "session_count",
                    "value": session_count,
                    "unit": "count",
                    "confidence": "observed" if session_count > 0 else "missing",
                },
                {
                    "key": "skipped_count",
                    "value": total_skipped,
                    "unit": "count",
                    "confidence": "observed" if total_skipped > 0 else "missing",
                },
                {
                    "key": "late_night_minutes",
                    "value": round(late_night_minutes, 2),
                    "unit": "minutes",
                    "confidence": "observed" if late_night_minutes > 0 else "missing",
                },
            ],
            "sessions": [s.to_dict() for s in sessions],
            "privacy_level": "safe_for_user_dm",
        }

    # ── Confidence ────────────────────────────────────────────────────

    @staticmethod
    def _determine_confidence(plays_on_date: int) -> str:
        if plays_on_date >= 10:
            return "high"
        if plays_on_date >= 5:
            return "medium"
        if plays_on_date >= 1:
            return "low"
        return "needs_review"

    # ── Narrative ─────────────────────────────────────────────────────

    @staticmethod
    def _build_narrative(
        sessions: list[SpotifySession],
        total_minutes: float,
        session_count: int,
        top_artists: list[str],
    ) -> str:
        if not sessions:
            return "No Spotify listening data available for this day."

        parts: list[str] = []
        parts.append(f"{total_minutes:.0f} min listened")
        if session_count > 1:
            parts.append(f"across {session_count} sessions")
        elif session_count == 1:
            parts.append("in 1 session")

        if top_artists:
            artist_str = ", ".join(top_artists[:3])
            parts.append(f"top artists: {artist_str}")

        late_sessions = [s for s in sessions if s.late_night]
        if late_sessions:
            late_mins = sum(s.total_minutes for s in late_sessions)
            parts.append(f"including {late_mins:.0f} min late-night")

        return "; ".join(parts) + "."

    # ── Convenience write methods ──────────────────────────────────────

    def write_json(self, data_dir: Path | str | None = None) -> Path:
        """Build the report and write it as JSON."""
        return write_report_json(
            self.build(), "spotify_daily", self.report_date.isoformat(), data_dir
        )

    def write_markdown(self, data_dir: Path | str | None = None) -> Path:
        """Build the report and write it as Markdown."""
        return write_report_markdown(
            self.build(), "spotify_daily", self.report_date.isoformat(), data_dir
        )


# ── Markdown rendering ──────────────────────────────────────────────────


def render_spotify_markdown(data: dict[str, Any]) -> str:
    """Render a Spotify daily summary report dict to Markdown."""
    lines: list[str] = []

    date_str = data.get("period_start", "unknown date")
    lines.append(f"# 🎵 Spotify Daily — {date_str}")
    lines.append("")

    summary = data.get("summary", "")
    if summary:
        lines.append(f"> **Summary**: {summary}")
        lines.append("")

    confidence = data.get("confidence", "unknown")
    lines.append(f"- **Confidence**: {confidence}")
    lines.append("")

    items = data.get("items", [])
    if items:
        lines.append("## Metrics")
        lines.append("")
        for item in items:
            key = item.get("key", "")
            value = item.get("value")
            unit = item.get("unit", "")
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            lines.append(f"- **{key}**: {value} {unit}")
        lines.append("")

    sessions = data.get("sessions", [])
    if sessions:
        lines.append("## Sessions")
        lines.append("")
        for i, s in enumerate(sessions, 1):
            start = s.get("start", "?")
            end = s.get("end", "?")
            minutes = s.get("total_minutes", 0)
            tracks = s.get("track_count", 0)
            artists = ", ".join(s.get("dominant_artists", []))
            late = " 🌙" if s.get("late_night") else ""
            lines.append(
                f"{i}. {start} → {end} | {minutes:.0f} min, {tracks} tracks | {artists}{late}"
            )
        lines.append("")

    lines.append("---")
    lines.append(
        f"*Generated at {data.get('generated_at', 'unknown')} — "
        f"privacy: {data.get('privacy_level', 'unknown')}*"
    )
    lines.append("")

    return "\n".join(lines)