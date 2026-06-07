from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from helios.modules.work_hours import WorkHoursAnalyzer, WorkHoursModule


def _write_history(path: Path, rows: list[tuple[str, float, float, float]]):
    with path.open("w") as f:
        for ts, lat, lon, acc in rows:
            f.write(json.dumps({
                "ts": ts,
                "lat": lat,
                "lon": lon,
                "accuracy": acc,
                "source": "test",
            }) + "\n")


def _cfg(tmp_path: Path, history: Path | None = None) -> dict:
    return {
        "timezone": "America/Edmonton",
        "history_path": str(history or tmp_path / "location_history.jsonl"),
        "state_path": str(tmp_path / "work_hours_state.json"),
        "overrides_path": str(tmp_path / "work_hours_overrides.json"),
        "home": {"lat": 51.1650, "lon": -113.9612},
        "anchor_start": "2026-05-11",
        "period_days": 14,
        "fixed_start": "07:00",
        "start_mode": "fixed",
        "round_minutes": 30,
        "end_rounding": "floor",
    }


def test_period_for_date_uses_biweekly_monday_to_friday_anchor(tmp_path):
    analyzer = WorkHoursAnalyzer(_cfg(tmp_path))

    start, end = analyzer.period_for_date(datetime(2026, 5, 25, tzinfo=timezone.utc).date())

    assert start.isoformat() == "2026-05-25"
    assert end.isoformat() == "2026-06-05"
    assert analyzer.format_period_label(start, end) == "May 25 - June 5"


def test_analyze_day_uses_dominant_worksite_cluster_not_commute_home_arrival(tmp_path):
    history = tmp_path / "location_history.jsonl"
    # UTC timestamps are MDT + 6. Worksite remains stable until 14:09 MDT;
    # later samples are commute/home and must not extend the paid end time.
    _write_history(history, [
        ("2026-05-22T06:00:00+00:00", 51.1650, -113.9612, 5),   # midnight home
        ("2026-05-22T13:30:00+00:00", 51.1000, -114.2300, 5),   # commute
        ("2026-05-22T14:05:00+00:00", 50.9530, -114.1050, 5),   # work 08:05
        ("2026-05-22T16:00:00+00:00", 50.9532, -114.1051, 5),
        ("2026-05-22T18:00:00+00:00", 50.9531, -114.1050, 5),
        ("2026-05-22T20:09:00+00:00", 50.9532, -114.1052, 5),   # work 14:09
        ("2026-05-22T20:36:00+00:00", 51.1600, -113.9700, 5),   # commute near home
        ("2026-05-22T20:47:00+00:00", 51.1650, -113.9612, 5),   # home 14:47
    ])
    analyzer = WorkHoursAnalyzer(_cfg(tmp_path, history))
    samples = analyzer.load_history()

    result = analyzer.analyze_day(
        datetime(2026, 5, 22).date(),
        samples,
        (51.1650, -113.9612),
        {},
    )

    assert result["start"] == "07:00"
    assert result["end"] == "14:00"
    assert result["line"] == "May 22 7am-2pm"
    assert result["confidence"] == "medium"
    assert result["evidence"]["cluster_samples"] == 4


# ---------------------------------------------------------------------------
# Epic 1 — confidence_reason, source, evidence_summary on every day result
# ---------------------------------------------------------------------------

def test_analyze_day_includes_confidence_reason_source_and_evidence_summary(tmp_path):
    history = tmp_path / "location_history.jsonl"
    _write_history(history, [
        ("2026-05-22T14:05:00+00:00", 50.9530, -114.1050, 5),
        ("2026-05-22T16:00:00+00:00", 50.9532, -114.1051, 5),
        ("2026-05-22T18:00:00+00:00", 50.9531, -114.1050, 5),
        ("2026-05-22T20:09:00+00:00", 50.9532, -114.1052, 5),
    ])
    analyzer = WorkHoursAnalyzer(_cfg(tmp_path, history))
    samples = analyzer.load_history()

    result = analyzer.analyze_day(
        datetime(2026, 5, 22).date(),
        samples,
        (51.1650, -113.9612),
        {},
    )

    assert "confidence_reason" in result
    assert "source" in result
    assert "evidence_summary" in result
    assert result["source"] == "location_inference"
    # evidence_summary should contain times and sample count, no raw coords
    assert "samples" in result["evidence_summary"]
    # No raw lat/lon floating-point numbers in evidence_summary
    assert "50.953" not in result["evidence_summary"]


def test_needs_review_day_has_source_and_confidence_reason(tmp_path):
    analyzer = WorkHoursAnalyzer(_cfg(tmp_path))

    result = analyzer.analyze_day(
        datetime(2026, 5, 22).date(),
        [],  # no samples
        None,  # home couldn't be inferred
        {},
    )

    assert result["kind"] == "needs_review"
    assert result["source"] == "needs_review"
    assert result["confidence_reason"].startswith("needs review:")
    assert result["confidence"] == "needs_review"


# ---------------------------------------------------------------------------
# Epic 1 — Task 1.1: manual overrides render clean lines and carry metadata
# ---------------------------------------------------------------------------

def test_overrides_render_paid_full_days_and_holidays(tmp_path):
    overrides = {
        "dates": {
            "2026-05-13": {
                "kind": "paid_full",
                "start": "07:00",
                "end": "15:00",
                "paid_hours": 8,
                "note": "PAID FULL 8 HOURS",
            },
            "2026-05-18": {"kind": "holiday", "label": "HOLIDAY"},
        }
    }
    overrides_path = tmp_path / "work_hours_overrides.json"
    overrides_path.write_text(json.dumps(overrides))
    cfg = _cfg(tmp_path)
    cfg["overrides_path"] = str(overrides_path)
    analyzer = WorkHoursAnalyzer(cfg)

    report = analyzer.analyze_period(
        datetime(2026, 5, 11).date(),
        datetime(2026, 5, 22).date(),
        samples=[],
        overrides=overrides,
    )

    may13 = next(d for d in report["days"] if d["date"] == "2026-05-13")
    may18 = next(d for d in report["days"] if d["date"] == "2026-05-18")
    assert may13["line"] == "May 13 7am-3pm - PAID FULL 8 HOURS"
    assert may13["paid_hours"] == 8
    assert may18["line"] == "May 18 HOLIDAY"
    assert may18["confidence"] == "manual"
    # Epic 1: override days carry source and evidence_summary
    assert may13["source"] == "manual_override"
    assert "manual override" in may13["confidence_reason"]
    assert "manual override" in may13["evidence_summary"]
    assert may18["source"] == "manual_override"
    assert may18["evidence_summary"] == "holiday: HOLIDAY"


# ---------------------------------------------------------------------------
# Epic 1 — Task 1.1: review metadata present and correct
# ---------------------------------------------------------------------------

def test_analyze_period_includes_review_metadata(tmp_path):
    history = tmp_path / "location_history.jsonl"
    _write_history(history, [
        ("2026-05-22T14:05:00+00:00", 50.9530, -114.1050, 5),
        ("2026-05-22T16:00:00+00:00", 50.9532, -114.1051, 5),
        ("2026-05-22T18:00:00+00:00", 50.9531, -114.1050, 5),
        ("2026-05-22T20:09:00+00:00", 50.9532, -114.1052, 5),
    ])
    overrides = {
        "dates": {
            "2026-05-13": {
                "kind": "paid_full",
                "start": "07:00",
                "end": "15:00",
                "paid_hours": 8,
                "note": "PAID FULL",
            },
        }
    }
    analyzer = WorkHoursAnalyzer(_cfg(tmp_path, history))

    report = analyzer.analyze_period(
        datetime(2026, 5, 11).date(),
        datetime(2026, 5, 22).date(),
        samples=analyzer.load_history(),
        overrides=overrides,
    )

    review = report["review"]
    assert "needs_review_count" in review
    assert "manual_override_count" in review
    assert "low_confidence_count" in review
    assert "missing_days" in review
    assert "next_pay_period_due" in review
    assert "copy_paste_ready" in review
    assert isinstance(review["missing_days"], list)
    # One override was supplied → manual_override_count == 1
    assert review["manual_override_count"] == 1


def test_review_metadata_copy_paste_ready_false_when_needs_review(tmp_path):
    """When there's a needs_review day, copy_paste_ready must be False."""
    analyzer = WorkHoursAnalyzer(_cfg(tmp_path))

    report = analyzer.analyze_period(
        datetime(2026, 5, 11).date(),
        datetime(2026, 5, 22).date(),
        samples=[],      # no data → every weekday will be needs_review or no-home
        overrides={"dates": {}},
    )

    assert report["review"]["needs_review_count"] > 0
    assert report["review"]["copy_paste_ready"] is False


# ---------------------------------------------------------------------------
# Epic 1 — Task 1.3: report_text remains copy-paste compatible
# ---------------------------------------------------------------------------

def test_report_text_remains_copy_paste_compatible(tmp_path):
    history = tmp_path / "location_history.jsonl"
    _write_history(history, [
        ("2026-05-22T14:05:00+00:00", 50.9530, -114.1050, 5),
        ("2026-05-22T16:00:00+00:00", 50.9532, -114.1051, 5),
        ("2026-05-22T18:00:00+00:00", 50.9531, -114.1050, 5),
        ("2026-05-22T20:09:00+00:00", 50.9532, -114.1052, 5),
    ])
    analyzer = WorkHoursAnalyzer(_cfg(tmp_path, history))

    report = analyzer.analyze_period(
        datetime(2026, 5, 11).date(),
        datetime(2026, 5, 22).date(),
        samples=analyzer.load_history(),
        overrides={"dates": {}},
    )

    text = report["report_text"]
    # Should start with the period label line
    assert text.startswith("May 11")
    # No JSON leakage in report_text
    assert "confidence_reason" not in text
    assert "evidence_summary" not in text
    assert '"source"' not in text


# ---------------------------------------------------------------------------
# Epic 1 — Task 1.3: future days are not listed as missing
# ---------------------------------------------------------------------------

def test_future_days_not_listed_as_missing(tmp_path):
    """Days that are still in the future should not appear in missing_days."""
    analyzer = WorkHoursAnalyzer(_cfg(tmp_path))

    # Analyze period 2026-05-25 to 2026-06-05, but set 'through' to a past
    # date so only a few days are analyzed.  Days after 'through' must not be
    # in missing_days even though they have no data yet.
    through_date = datetime(2026, 5, 27).date()

    report = analyzer.analyze_period(
        datetime(2026, 5, 25).date(),
        datetime(2026, 6, 5).date(),
        samples=[],
        overrides={"dates": {}},
        through=through_date,
    )

    # Days after the through-date must NOT appear in missing_days
    for missing in report["review"]["missing_days"]:
        missing_date = datetime.fromisoformat(missing).date()
        assert missing_date <= through_date, (
            f"future day {missing} should not be in missing_days"
        )


# ---------------------------------------------------------------------------
# Module tick still works and review metadata is written to state
# ---------------------------------------------------------------------------

def test_module_writes_state_and_marks_notification_sent(tmp_path):
    history = tmp_path / "location_history.jsonl"
    _write_history(history, [
        ("2026-05-22T14:05:00+00:00", 50.9530, -114.1050, 5),
        ("2026-05-22T16:00:00+00:00", 50.9532, -114.1051, 5),
        ("2026-05-22T18:00:00+00:00", 50.9531, -114.1050, 5),
        ("2026-05-22T20:09:00+00:00", 50.9532, -114.1052, 5),
    ])
    cfg = _cfg(tmp_path, history)
    cfg["notify_enabled"] = False
    module = WorkHoursModule(db_path=None, config=cfg)

    result = module.tick()
    module.mark_notification_sent(result["notify_key"])

    state = json.loads((tmp_path / "work_hours_state.json").read_text())
    assert state["period_start"] == "2026-05-25"
    assert state["period_end"] == "2026-06-05"
    assert state["last_notified_key"] == result["notify_key"]
    assert "report_text" in state
    # Epic 1: review metadata is persisted in state
    assert "review" in state
    assert "needs_review_count" in state["review"]
    assert "copy_paste_ready" in state["review"]


def test_tick_result_includes_review_key(tmp_path):
    history = tmp_path / "location_history.jsonl"
    _write_history(history, [
        ("2026-05-22T14:05:00+00:00", 50.9530, -114.1050, 5),
        ("2026-05-22T16:00:00+00:00", 50.9532, -114.1051, 5),
        ("2026-05-22T18:00:00+00:00", 50.9531, -114.1050, 5),
        ("2026-05-22T20:09:00+00:00", 50.9532, -114.1052, 5),
    ])
    cfg = _cfg(tmp_path, history)
    cfg["notify_enabled"] = False
    module = WorkHoursModule(db_path=None, config=cfg)

    result = module.tick()

    assert "review" in result
    assert isinstance(result["review"], dict)
    assert "needs_review_count" in result["review"]