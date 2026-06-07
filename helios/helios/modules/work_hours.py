"""Helios v6 — Work hours timesheet module.

Turns Helios location history into a reviewable bi-weekly work-hours draft.
The module is deterministic and privacy-local: it reads only local JSON/JSONL
state, writes a local state export, and asks the engine to notify the user when
an anchored pay period is ready for review.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from .base import BaseMod

logger = logging.getLogger("helios.work_hours")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
DEFAULT_HISTORY_PATH = DATA_DIR / "location_history.jsonl"
DEFAULT_STATE_PATH = DATA_DIR / "work_hours_state.json"
DEFAULT_OVERRIDES_PATH = DATA_DIR / "work_hours_overrides.json"
DEFAULT_TZ = "America/Edmonton"


@dataclass(frozen=True)
class LocationSample:
    ts_utc: datetime
    ts_local: datetime
    lat: float
    lon: float
    accuracy: float | None = None
    source: str = "unknown"


@dataclass
class WorkCluster:
    key: tuple[float, float]
    samples: list[LocationSample]

    @property
    def first(self) -> datetime:
        return min(s.ts_local for s in self.samples)

    @property
    def last(self) -> datetime:
        return max(s.ts_local for s in self.samples)

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def span_minutes(self) -> float:
        return max(0.0, (self.last - self.first).total_seconds() / 60.0)

    @property
    def score(self) -> float:
        # Favour sustained stationary clusters over drive-by transit samples.
        return self.count * 3.0 + self.span_minutes / 10.0


class WorkHoursAnalyzer:
    """Deterministic location-history analyzer for payroll drafts."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.tz = ZoneInfo(self.config.get("timezone", DEFAULT_TZ))
        self.history_path = Path(
            os.path.expanduser(str(self.config.get("history_path", DEFAULT_HISTORY_PATH)))
        )
        self.overrides_path = Path(
            os.path.expanduser(str(self.config.get("overrides_path", DEFAULT_OVERRIDES_PATH)))
        )
        self.home_radius_m = float(self.config.get("home_radius_m", 600))
        self.workday_start = self._parse_hhmm(self.config.get("workday_window_start", "06:00"))
        self.workday_end = self._parse_hhmm(self.config.get("workday_window_end", "18:00"))
        self.fixed_start = str(self.config.get("fixed_start", "07:00"))
        self.start_mode = str(self.config.get("start_mode", "fixed"))
        self.round_minutes = int(self.config.get("round_minutes", 30))
        self.rounding = str(self.config.get("end_rounding", "floor"))
        self.max_accuracy_m = float(self.config.get("max_accuracy_m", 250))
        self.cluster_precision = int(self.config.get("cluster_precision", 2))
        self.min_cluster_minutes = float(self.config.get("min_cluster_minutes", 45))
        self.min_cluster_samples = int(self.config.get("min_cluster_samples", 3))
        self.include_weekends = bool(self.config.get("include_weekends", False))

    # ------------------------------------------------------------------
    # Loading and primitives
    # ------------------------------------------------------------------

    def load_history(self) -> list[LocationSample]:
        if not self.history_path.exists():
            return []
        samples: list[LocationSample] = []
        with self.history_path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    lat = raw.get("lat")
                    lon = raw.get("lon")
                    if lat is None or lon is None:
                        continue
                    accuracy = raw.get("accuracy")
                    if accuracy is not None and float(accuracy) > self.max_accuracy_m:
                        continue
                    ts = datetime.fromisoformat(str(raw["ts"]).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    ts_utc = ts.astimezone(timezone.utc)
                    samples.append(LocationSample(
                        ts_utc=ts_utc,
                        ts_local=ts_utc.astimezone(self.tz),
                        lat=float(lat),
                        lon=float(lon),
                        accuracy=float(accuracy) if accuracy is not None else None,
                        source=str(raw.get("source", "unknown")),
                    ))
                except Exception as exc:
                    logger.debug("Skipping malformed location row: %s", exc)
        samples.sort(key=lambda s: s.ts_utc)
        return samples

    def load_overrides(self) -> dict[str, Any]:
        if not self.overrides_path.exists():
            return {"dates": {}}
        try:
            data = json.loads(self.overrides_path.read_text())
            if "dates" not in data:
                data = {"dates": data}
            return data
        except Exception as exc:
            logger.warning("Failed to read work-hours overrides: %s", exc)
            return {"dates": {}}

    def infer_home(self, samples: list[LocationSample]) -> tuple[float, float] | None:
        cfg_home = self.config.get("home") or {}
        if cfg_home.get("lat") is not None and cfg_home.get("lon") is not None:
            return float(cfg_home["lat"]), float(cfg_home["lon"])

        night = [
            s for s in samples
            if s.ts_local.hour < 5 or s.ts_local.hour >= 23
        ]
        if len(night) < 5:
            night = samples
        if not night:
            return None
        return median([s.lat for s in night]), median([s.lon for s in night])

    @staticmethod
    def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
        radius = 6_371_000.0
        lat1, lon1 = math.radians(a[0]), math.radians(a[1])
        lat2, lon2 = math.radians(b[0]), math.radians(b[1])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        hav = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        )
        return 2 * radius * math.asin(math.sqrt(hav))

    @staticmethod
    def _parse_hhmm(value: str) -> time:
        hour, minute = [int(part) for part in str(value).split(":", 1)]
        return time(hour=hour, minute=minute)

    @staticmethod
    def _date_range(start: date, end: date) -> list[date]:
        days = []
        cur = start
        while cur <= end:
            days.append(cur)
            cur += timedelta(days=1)
        return days

    # ------------------------------------------------------------------
    # Period math
    # ------------------------------------------------------------------

    def period_for_date(self, today: date | None = None) -> tuple[date, date]:
        today = today or datetime.now(self.tz).date()
        anchor = date.fromisoformat(str(self.config.get("anchor_start", "2026-05-11")))
        period_days = int(self.config.get("period_days", 14))
        offset_days = (today - anchor).days
        period_index = math.floor(offset_days / period_days)
        start = anchor + timedelta(days=period_index * period_days)
        end = start + timedelta(days=period_days - 3)  # Monday→following Friday
        return start, end

    def notification_due(
        self, now: datetime | None = None, last_notified_key: str | None = None
    ) -> tuple[bool, str, tuple[date, date]]:
        now_local = (now or datetime.now(timezone.utc)).astimezone(self.tz)
        start, end = self.period_for_date(now_local.date())
        notify_hour = int(self.config.get("notify_hour", 17))
        key = f"{start.isoformat()}_{end.isoformat()}"
        if key == last_notified_key:
            return False, key, (start, end)
        if now_local.date() > end:
            return True, key, (start, end)
        if now_local.date() == end and now_local.hour >= notify_hour:
            return True, key, (start, end)
        return False, key, (start, end)

    # ------------------------------------------------------------------
    # Daily analysis
    # ------------------------------------------------------------------

    def analyze_period(
        self,
        start: date,
        end: date,
        samples: list[LocationSample] | None = None,
        overrides: dict[str, Any] | None = None,
        through: date | None = None,
    ) -> dict[str, Any]:
        samples = samples if samples is not None else self.load_history()
        overrides = overrides if overrides is not None else self.load_overrides()
        home = self.infer_home(samples)
        days: list[dict[str, Any]] = []
        analysis_end = min(end, through) if through is not None else end
        for day in self._date_range(start, analysis_end):
            if day.weekday() >= 5 and not self.include_weekends:
                continue
            day_samples = [s for s in samples if s.ts_local.date() == day]
            day_override = (overrides.get("dates") or {}).get(day.isoformat(), {})
            days.append(self.analyze_day(day, day_samples, home, day_override))

        report_text = self.format_report(start, end, days)
        confidence_summary: dict[str, int] = {}
        paid_total = 0.0
        for item in days:
            confidence_summary[item["confidence"]] = confidence_summary.get(item["confidence"], 0) + 1
            paid_total += float(item.get("paid_hours") or 0.0)

        review = self._build_review_metadata(days, start, end, through)

        return {
            "schema_version": "work_hours.v1",
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "period_label": self.format_period_label(start, end),
            "home_inferred": home is not None,
            "days": days,
            "report_text": report_text,
            "paid_hours_total": round(paid_total, 2),
            "confidence_summary": confidence_summary,
            "review": review,
        }

    def _build_review_metadata(
        self,
        days: list[dict[str, Any]],
        start: date,
        end: date,
        through: date | None = None,
    ) -> dict[str, Any]:
        """Build review metadata for the period's state output."""
        needs_review_count = sum(1 for d in days if d.get("kind") == "needs_review")
        manual_override_count = sum(1 for d in days if d.get("source") == "manual_override")
        low_confidence_count = sum(1 for d in days if d.get("confidence") == "low")

        # Determine missing weekdays — days within the period that have no
        # day result *and* are in the past relative to the analysis cutoff.
        analysis_end = min(end, through) if through is not None else end
        observed_dates = {d["date"] for d in days}
        today = datetime.now(self.tz).date()
        missing: list[str] = []
        for day in self._date_range(start, analysis_end):
            if day.weekday() >= 5 and not self.include_weekends:
                continue
            if day.isoformat() not in observed_dates and day <= today:
                missing.append(day.isoformat())

        # Next pay-period due date
        period_days = int(self.config.get("period_days", 14))
        next_start = start + timedelta(days=period_days)
        next_end = next_start + timedelta(days=period_days - 3)
        next_pay_period_due = next_end.isoformat()

        copy_paste_ready = needs_review_count == 0 and low_confidence_count == 0

        return {
            "needs_review_count": needs_review_count,
            "manual_override_count": manual_override_count,
            "low_confidence_count": low_confidence_count,
            "missing_days": missing,
            "next_pay_period_due": next_pay_period_due,
            "copy_paste_ready": copy_paste_ready,
        }

    def analyze_day(
        self,
        day: date,
        samples: list[LocationSample],
        home: tuple[float, float] | None,
        override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        override = override or {}
        if override:
            return self._day_from_override(day, override)

        if home is None:
            return self._needs_review(day, "home location could not be inferred", samples)

        windowed = [
            s for s in samples
            if self.workday_start <= s.ts_local.time() <= self.workday_end
        ]
        away = [
            s for s in windowed
            if self.distance_m((s.lat, s.lon), home) > self.home_radius_m
        ]
        if not away:
            return self._needs_review(day, "no away-from-home work window detected", samples)

        cluster = self._dominant_work_cluster(away)
        if cluster is None:
            return self._needs_review(day, "away data was transit-like; no stable worksite cluster", samples)

        if self.start_mode == "observed":
            start_dt = self._round_datetime(cluster.first, mode="floor")
            start_label = self._time_to_hhmm(start_dt.time())
        else:
            start_label = self.fixed_start

        end_dt = self._round_datetime(cluster.last, mode=self.rounding)
        end_label = self._time_to_hhmm(end_dt.time())
        paid_hours = self._hours_between(start_label, end_label)
        confidence = self._confidence_for(cluster, samples)

        evidence_summary = (
            f"stable worksite cluster "
            f"{self._time_to_hhmm(cluster.first.time())}-{self._time_to_hhmm(cluster.last.time())}, "
            f"{cluster.count} samples"
        )
        confidence_reason = self._confidence_reason_for(cluster, confidence)

        return {
            "date": day.isoformat(),
            "kind": "work",
            "start": start_label,
            "end": end_label,
            "paid_hours": paid_hours,
            "confidence": confidence,
            "confidence_reason": confidence_reason,
            "source": "location_inference",
            "evidence_summary": evidence_summary,
            "note": "",
            "line": self.format_day_line(day, start_label, end_label, ""),
            "evidence": {
                "worksite_key": list(cluster.key),
                "observed_first": cluster.first.isoformat(),
                "observed_last": cluster.last.isoformat(),
                "cluster_samples": cluster.count,
                "cluster_span_minutes": round(cluster.span_minutes, 1),
                "day_samples": len(samples),
                "max_workday_gap_minutes": round(self._max_gap_minutes(windowed), 1),
            },
        }

    def _confidence_reason_for(self, cluster: WorkCluster, confidence: str) -> str:
        if confidence == "high":
            return f"high confidence: cluster spans {int(cluster.span_minutes)}min with {cluster.count} samples"
        if confidence == "medium":
            return f"medium confidence: cluster spans {int(cluster.span_minutes)}min with {cluster.count} samples"
        return f"low confidence: cluster spans {int(cluster.span_minutes)}min with {cluster.count} samples"

    def _day_from_override(self, day: date, override: dict[str, Any]) -> dict[str, Any]:
        kind = str(override.get("kind", "work"))
        if kind == "holiday":
            label = str(override.get("label", "HOLIDAY"))
            return {
                "date": day.isoformat(),
                "kind": "holiday",
                "paid_hours": float(override.get("paid_hours", 0)),
                "confidence": "manual",
                "confidence_reason": "manual override: holiday",
                "source": "manual_override",
                "evidence_summary": f"holiday: {label}",
                "note": label,
                "line": f"{self.format_day(day)} {label}",
                "evidence": {"override": True},
            }

        start = str(override.get("start", self.fixed_start))
        end = str(override.get("end", "15:00"))
        note = str(override.get("note", ""))
        paid_hours = float(override.get("paid_hours", self._hours_between(start, end)))
        summary = f"manual override: {start}-{end}"
        if note:
            summary = f"manual override: {note}"
        return {
            "date": day.isoformat(),
            "kind": kind,
            "start": start,
            "end": end,
            "paid_hours": paid_hours,
            "confidence": "manual",
            "confidence_reason": f"manual override: {kind}",
            "source": "manual_override",
            "evidence_summary": summary,
            "note": note,
            "line": self.format_day_line(day, start, end, note),
            "evidence": {"override": True},
        }

    def _needs_review(
        self, day: date, reason: str, samples: list[LocationSample]
    ) -> dict[str, Any]:
        return {
            "date": day.isoformat(),
            "kind": "needs_review",
            "paid_hours": 0.0,
            "confidence": "needs_review",
            "confidence_reason": f"needs review: {reason}",
            "source": "needs_review",
            "evidence_summary": f"{len(samples)} samples; {reason}",
            "note": reason,
            "line": f"{self.format_day(day)} NEEDS REVIEW - {reason}",
            "evidence": {"day_samples": len(samples)},
        }

    def _dominant_work_cluster(self, away: list[LocationSample]) -> WorkCluster | None:
        groups: dict[tuple[float, float], list[LocationSample]] = {}
        for sample in away:
            key = (round(sample.lat, self.cluster_precision), round(sample.lon, self.cluster_precision))
            groups.setdefault(key, []).append(sample)
        clusters = [WorkCluster(key, values) for key, values in groups.items()]
        clusters = [
            c for c in clusters
            if c.count >= self.min_cluster_samples or c.span_minutes >= self.min_cluster_minutes
        ]
        if not clusters:
            return None
        return max(clusters, key=lambda c: c.score)

    def _confidence_for(self, cluster: WorkCluster, samples: list[LocationSample]) -> str:
        if cluster.span_minutes >= 240 and cluster.count >= 8:
            return "high"
        if cluster.span_minutes >= 120 and cluster.count >= 4:
            return "medium"
        return "low"

    def _max_gap_minutes(self, samples: list[LocationSample]) -> float:
        if len(samples) < 2:
            return 0.0
        ordered = sorted(samples, key=lambda s: s.ts_local)
        return max(
            (b.ts_local - a.ts_local).total_seconds() / 60.0
            for a, b in zip(ordered, ordered[1:])
        )

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format_report(self, start: date, end: date, days: list[dict[str, Any]]) -> str:
        first_week: list[str] = []
        second_week: list[str] = []
        split = start + timedelta(days=7)
        for item in days:
            day = date.fromisoformat(item["date"])
            (first_week if day < split else second_week).append(item["line"])
        lines = [self.format_period_label(start, end), "", *first_week]
        if second_week:
            lines.extend(["", *second_week])
        return "\n".join(lines).strip()

    @staticmethod
    def format_period_label(start: date, end: date) -> str:
        return f"{start.strftime('%B')} {start.day} - {end.strftime('%B')} {end.day}"

    @staticmethod
    def format_day(day: date) -> str:
        return f"{day.strftime('%B')} {day.day}"

    def format_day_line(self, day: date, start: str, end: str, note: str = "") -> str:
        base = f"{self.format_day(day)} {self.format_time_label(start)}-{self.format_time_label(end)}"
        if note:
            return f"{base} - {note}"
        return base

    @staticmethod
    def _time_to_hhmm(value: time) -> str:
        return f"{value.hour:02d}:{value.minute:02d}"

    @staticmethod
    def format_time_label(value: str) -> str:
        hour, minute = [int(part) for part in str(value).split(":", 1)]
        suffix = "am" if hour < 12 else "pm"
        hour12 = hour % 12 or 12
        if minute == 0:
            return f"{hour12}{suffix}"
        return f"{hour12}:{minute:02d}{suffix}"

    def _round_datetime(self, dt: datetime, mode: str = "floor") -> datetime:
        step = max(1, self.round_minutes)
        minute_bucket = dt.hour * 60 + dt.minute
        if mode == "ceil":
            rounded = math.ceil(minute_bucket / step) * step
        elif mode == "nearest":
            rounded = round(minute_bucket / step) * step
        else:
            rounded = math.floor(minute_bucket / step) * step
        rounded = max(0, min(23 * 60 + 59, rounded))
        return dt.replace(hour=rounded // 60, minute=rounded % 60, second=0, microsecond=0)

    def _hours_between(self, start: str, end: str) -> float:
        s = self._parse_hhmm(start)
        e = self._parse_hhmm(end)
        start_minutes = s.hour * 60 + s.minute
        end_minutes = e.hour * 60 + e.minute
        return round(max(0, end_minutes - start_minutes) / 60.0, 2)


class WorkHoursModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "work_hours",
        "version": "1.0.0",
        "description": "Tracks work hours from calendar events",
        "author": "Helios",
        "collectors": ["location_history.jsonl"],
        "dependencies": [],
        "priority": 6,
    }

    def __init__(self, db_path: str | None = None, config: dict | None = None) -> None:
        super().__init__(db_path=db_path, config=config)
        self.config = config or {}
        self.analyzer = WorkHoursAnalyzer(self.config)
        self.state_path = Path(
            os.path.expanduser(str(self.config.get("state_path", DEFAULT_STATE_PATH)))
        )

    def tick(self) -> dict[str, Any]:
        state = self._read_state()
        now = datetime.now(timezone.utc)
        now_local = now.astimezone(self.analyzer.tz)
        last_key = state.get("last_notified_key")
        due, notify_key, (start, end) = self.analyzer.notification_due(now, last_key)
        through = end if due or now_local.date() > end else min(now_local.date(), end)
        report = self.analyzer.analyze_period(start, end, through=through)
        report.update({
            "generated_at": now.isoformat(),
            "should_notify": bool(self.config.get("notify_enabled", True) and due),
            "notify_key": notify_key,
            "next_report_due": self._next_report_due(start).isoformat(),
        })
        self._write_state(report, previous_state=state)
        return {
            "period_label": report["period_label"],
            "period_start": report["period_start"],
            "period_end": report["period_end"],
            "paid_hours_total": report["paid_hours_total"],
            "confidence_summary": report["confidence_summary"],
            "report_text": report["report_text"],
            "review": report["review"],
            "should_notify": report["should_notify"],
            "notify_key": notify_key,
            "state_path": str(self.state_path),
        }

    def mark_notification_sent(self, notify_key: str) -> None:
        state = self._read_state()
        state["last_notified_key"] = notify_key
        state["last_notified_at"] = datetime.now(timezone.utc).isoformat()
        self._atomic_write_json(self.state_path, state)

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {}

    def _write_state(self, report: dict[str, Any], previous_state: dict[str, Any]) -> None:
        state = dict(report)
        for key in ("last_notified_key", "last_notified_at"):
            if previous_state.get(key):
                state[key] = previous_state[key]
        self._atomic_write_json(self.state_path, state)

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        tmp.replace(path)

    def _next_report_due(self, current_start: date) -> datetime:
        period_days = int(self.config.get("period_days", 14))
        notify_hour = int(self.config.get("notify_hour", 17))
        next_start = current_start + timedelta(days=period_days)
        next_end = next_start + timedelta(days=period_days - 3)
        return datetime.combine(next_end, time(hour=notify_hour), tzinfo=self.analyzer.tz)
