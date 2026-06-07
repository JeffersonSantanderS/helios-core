"""Tests for helios.reports.health — Health diary report generation.

Epic 4 verification: JSON/Markdown health diary reports with semantic flags,
schema compliance, missing-value handling, and atomic writes.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from helios.reports import (
    REPORT_SCHEMA_VERSION,
    report_filename,
    write_report_json,
    write_report_markdown,
)
from helios.reports.health import (
    FORBIDDEN_DIAGNOSIS_WORDS,
    HealthDiaryReport,
    _build_narrative,
    _determine_confidence,
    _evaluate_flags,
    _find_gaps,
    _resolve_metric,
    contains_diagnosis_language,
    render_markdown,
)


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def rich_metrics() -> dict:
    """A full set of health metrics for a well-rested active day."""
    return {
        "sleep_hours": 7.5,
        "steps": 10500,
        "active_minutes": 45,
        "resting_hr": 62,
        "hrv_ms": 45,
        "workout_minutes": 30,
        "protein_g": 120,
        "mood_score": 8,
    }


@pytest.fixture
def sparse_metrics() -> dict:
    """Minimal metrics — only sleep and steps."""
    return {
        "sleep_hours": 6.0,
        "steps": 3000,
    }


@pytest.fixture
def empty_metrics() -> dict:
    """No metrics at all."""
    return {}


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    """Isolated temporary directory for report file writes."""
    d = tmp_path / "reports"
    d.mkdir()
    return d


# ── Test: Schema version ───────────────────────────────────────────────


def test_schema_version_is_report_v1():
    assert REPORT_SCHEMA_VERSION == "report.v1"


def test_report_has_correct_schema_version(rich_metrics):
    report = HealthDiaryReport(rich_metrics, report_date="2025-05-25")
    data = report.build()
    assert data["schema_version"] == "report.v1"


# ── Test: Missing values display as missing, NOT zero ──────────────────


def test_missing_sleep_shows_none_not_zero(sparse_metrics):
    """When sleep is missing it should be None, not 0."""
    del sparse_metrics["sleep_hours"]
    report = HealthDiaryReport(sparse_metrics, report_date="2025-05-25")
    data = report.build()
    for item in data["items"]:
        if item["key"] == "sleep_hours":
            assert item["value"] is None
            assert item["confidence"] == "missing"
            break
    else:
        pytest.fail("sleep_hours item not found")


def test_missing_active_minutes_shows_none(sparse_metrics):
    del sparse_metrics["steps"]
    report = HealthDiaryReport(sparse_metrics, report_date="2025-05-25")
    data = report.build()
    for item in data["items"]:
        if item["key"] == "steps":
            assert item["value"] is None


def test_empty_metrics_all_items_missing(empty_metrics):
    report = HealthDiaryReport(empty_metrics, report_date="2025-05-25")
    data = report.build()
    # All items should have value=None
    sleep_item = next(i for i in data["items"] if i["key"] == "sleep_hours")
    assert sleep_item["value"] is None
    steps_item = next(i for i in data["items"] if i["key"] == "steps")
    assert steps_item["value"] is None
    active_item = next(i for i in data["items"] if i["key"] == "active_minutes")
    assert active_item["value"] is None
    # No optional items present when missing
    optional_keys = {"resting_hr", "hrv_ms", "workout_minutes", "protein_g", "mood_score"}
    present_optional = {i["key"] for i in data["items"]} & optional_keys
    assert present_optional == set()


def test_gaps_list_populated_for_missing_metrics(empty_metrics):
    report = HealthDiaryReport(empty_metrics, report_date="2025-05-25")
    data = report.build()
    assert "sleep_hours" in data["gaps"]
    assert "steps" in data["gaps"]


# ── Test: No medical diagnosis language ───────────────────────────────


def test_no_diagnosis_language_in_rich_report(rich_metrics):
    report = HealthDiaryReport(rich_metrics, report_date="2025-05-25")
    data = report.build()
    full_text = json.dumps(data)
    assert not contains_diagnosis_language(full_text), (
        f"Report contains forbidden diagnosis language: "
        f"{[w for w in FORBIDDEN_DIAGNOSIS_WORDS if w in full_text.lower()]}"
    )


def test_no_diagnosis_language_in_flagged_report():
    """A report with low_sleep and high_resting_hr flags must still avoid diagnosis words."""
    metrics = {"sleep_hours": 4.0, "resting_hr": 88}
    report = HealthDiaryReport(metrics, report_date="2025-05-25")
    data = report.build()
    full_text = json.dumps(data)
    assert not contains_diagnosis_language(full_text)


def test_no_diagnosis_language_in_markdown(rich_metrics):
    report = HealthDiaryReport(rich_metrics, report_date="2025-05-25")
    data = report.build()
    md = render_markdown(data)
    assert not contains_diagnosis_language(md)


def test_summary_uses_observed_language():
    """Flag details must use 'observed with' language, not causation."""
    metrics = {"sleep_hours": 4.0}
    report = HealthDiaryReport(metrics, report_date="2025-05-25")
    data = report.build()
    for flag in data["flags"]:
        assert "observed with" in flag["detail"]


# ── Test: Low sleep flags ──────────────────────────────────────────────


def test_low_sleep_flag():
    metrics = {"sleep_hours": 4.5}
    flags = _evaluate_flags(metrics)
    assert len(flags) == 1
    assert flags[0]["flag"] == "low_sleep"
    assert flags[0]["metric"] == "sleep_hours"


def test_no_low_sleep_flag_at_5h():
    metrics = {"sleep_hours": 5.0}
    flags = _evaluate_flags(metrics)
    low_sleep_flags = [f for f in flags if f["flag"] == "low_sleep"]
    assert len(low_sleep_flags) == 0


def test_no_low_sleep_flag_normal():
    metrics = {"sleep_hours": 7.5}
    flags = _evaluate_flags(metrics)
    low_sleep_flags = [f for f in flags if f["flag"] == "low_sleep"]
    assert len(low_sleep_flags) == 0


# ── Test: High sleep flags ─────────────────────────────────────────────


def test_long_sleep_flag():
    metrics = {"sleep_hours": 11.0}
    flags = _evaluate_flags(metrics)
    assert any(f["flag"] == "long_sleep" for f in flags)


def test_no_long_sleep_flag_at_10h():
    metrics = {"sleep_hours": 10.0}
    flags = _evaluate_flags(metrics)
    long_flags = [f for f in flags if f["flag"] == "long_sleep"]
    assert len(long_flags) == 0


# ── Test: Activity flags ───────────────────────────────────────────────


def test_high_activity_flag():
    metrics = {"active_minutes": 75}
    flags = _evaluate_flags(metrics)
    assert any(f["flag"] == "high_activity" for f in flags)


def test_no_high_activity_flag_at_60():
    metrics = {"active_minutes": 60}
    flags = _evaluate_flags(metrics)
    activity_flags = [f for f in flags if f["flag"] == "high_activity"]
    assert len(activity_flags) == 0


def test_no_high_activity_flag_below_threshold():
    metrics = {"active_minutes": 30}
    flags = _evaluate_flags(metrics)
    activity_flags = [f for f in flags if f["flag"] == "high_activity"]
    assert len(activity_flags) == 0


# ── Test: HR flags ─────────────────────────────────────────────────────


def test_high_resting_hr_flag():
    metrics = {"resting_hr": 85}
    flags = _evaluate_flags(metrics)
    assert any(f["flag"] == "high_resting_hr" for f in flags)


def test_no_high_resting_hr_flag_at_80():
    metrics = {"resting_hr": 80}
    flags = _evaluate_flags(metrics)
    hr_flags = [f for f in flags if f["flag"] == "high_resting_hr"]
    assert len(hr_flags) == 0


def test_no_high_resting_hr_flag_normal():
    metrics = {"resting_hr": 65}
    flags = _evaluate_flags(metrics)
    hr_flags = [f for f in flags if f["flag"] == "high_resting_hr"]
    assert len(hr_flags) == 0


# ── Test: Multiple flags ──────────────────────────────────────────────


def test_multiple_flags_combined():
    metrics = {"sleep_hours": 4.0, "active_minutes": 90, "resting_hr": 88}
    flags = _evaluate_flags(metrics)
    flag_names = {f["flag"] for f in flags}
    assert flag_names == {"low_sleep", "high_activity", "high_resting_hr"}


# ── Test: Metric resolution with aliases ──────────────────────────────


def test_resolve_metric_with_dotted_alias():
    metrics = {"sleep.hours": 7.5}
    assert _resolve_metric(metrics, "sleep_hours") == 7.5


def test_resolve_metric_canonical_key():
    metrics = {"sleep_hours": 7.5}
    assert _resolve_metric(metrics, "sleep_hours") == 7.5


def test_resolve_metric_missing_returns_none():
    metrics = {"steps": 5000}
    assert _resolve_metric(metrics, "sleep_hours") is None


# ── Test: Confidence levels ────────────────────────────────────────────


def test_confidence_high_with_rich_data(rich_metrics):
    assert _determine_confidence(rich_metrics) == "high"


def test_confidence_medium_with_sparse(sparse_metrics):
    """sparse_metrics has 2 items (sleep_hours, steps) → 'low'.
    Add a third to reach 'medium'."""
    metrics = {**sparse_metrics, "active_minutes": 30}
    assert _determine_confidence(metrics) == "medium"

def test_confidence_low_with_two_metrics(sparse_metrics):
    """Only sleep_hours and steps = 2 keys → low."""
    assert _determine_confidence(sparse_metrics) == "low"


def test_confidence_low_with_one_metric():
    assert _determine_confidence({"sleep_hours": 7}) == "low"


def test_confidence_needs_review_with_nothing(empty_metrics):
    assert _determine_confidence(empty_metrics) == "needs_review"


def test_confidence_downgraded_for_missing_core():
    """High item count but missing sleep should downgrade to medium."""
    metrics = {"steps": 8000, "active_minutes": 45, "mood_score": 7, "protein_g": 100, "resting_hr": 62}
    # 5 items but sleep_hours is missing
    assert _determine_confidence(metrics) == "medium"


# ── Test: Report structure ────────────────────────────────────────────


def test_report_structure_keys(rich_metrics):
    data = HealthDiaryReport(rich_metrics, report_date="2025-05-25").build()
    required_keys = {
        "schema_version", "report_type", "period_start", "period_end",
        "generated_at", "confidence", "summary", "items", "gaps", "privacy_level",
    }
    assert required_keys.issubset(set(data.keys()))


def test_report_type_is_health(rich_metrics):
    data = HealthDiaryReport(rich_metrics, report_date="2025-05-25").build()
    assert data["report_type"] == "health"


def test_privacy_level(rich_metrics):
    data = HealthDiaryReport(rich_metrics, report_date="2025-05-25").build()
    assert data["privacy_level"] == "safe_for_user_dm"


def test_period_matches_date(rich_metrics):
    data = HealthDiaryReport(rich_metrics, report_date="2025-05-25").build()
    assert data["period_start"] == "2025-05-25"
    assert data["period_end"] == "2025-05-25"


# ── Test: Atomic JSON write ────────────────────────────────────────────


def test_write_json_atomic(rich_metrics, output_dir):
    report = HealthDiaryReport(rich_metrics, report_date="2025-05-25")
    path = report.write_json(data_dir=output_dir)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == "report.v1"
    assert data["report_type"] == "health"


def test_write_json_creates_file(rich_metrics, output_dir):
    """File must exist after write."""
    report = HealthDiaryReport(rich_metrics, report_date="2025-05-25")
    path = report.write_json(data_dir=output_dir)
    assert path.is_file()


def test_write_json_creates_parent_dirs(rich_metrics, tmp_path):
    """Atomic write should create missing directories."""
    deep_dir = tmp_path / "nested" / "reports"
    report = HealthDiaryReport(rich_metrics, report_date="2025-05-25")
    path = report.write_json(data_dir=deep_dir)
    assert path.is_file()


# ── Test: Markdown generation ──────────────────────────────────────────


def test_markdown_written_alongside_json(rich_metrics, output_dir):
    report = HealthDiaryReport(rich_metrics, report_date="2025-05-25")
    paths = report.write_all(data_dir=output_dir)
    md_path: Path = paths["markdown"]
    json_path: Path = paths["json"]
    assert md_path.exists()
    assert json_path.exists()

    md_text = md_path.read_text(encoding="utf-8")
    assert "Health Diary" in md_text
    assert "2025-05-25" in md_text


def test_markdown_content_structure(rich_metrics):
    data = HealthDiaryReport(rich_metrics, report_date="2025-05-25").build()
    md = render_markdown(data)
    assert "# 🩺 Health Diary" in md
    assert "Metrics" in md
    assert "Summary" in md


def test_markdown_shows_dash_for_missing(sparse_metrics):
    """Missing metrics should be displayed as '—', not as '0' or 'None'."""
    del sparse_metrics["sleep_hours"]
    data = HealthDiaryReport(sparse_metrics, report_date="2025-05-25").build()
    md = render_markdown(data)
    assert "—" in md  # em dash for missing values
    # Ensure "None" doesn't appear as a value in the table
    lines = md.split("\n")
    table_lines = [l for l in lines if "|" in l and "sleep_hours" in l]
    if table_lines:
        # Value cell should not be "None"
        cells = [c.strip() for c in table_lines[0].split("|")]
        assert "None" not in cells


# ── Test: report_filename helper ────────────────────────────────────────


def test_report_filename():
    assert report_filename("health_diary", "2025-05-25") == "health_diary_2025-05-25"


# ── Test: write_report_json helper directly ────────────────────────────


def test_write_report_json_direct(output_dir):
    data = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "health",
        "period_start": "2025-05-25",
        "period_end": "2025-05-25",
    }
    path = write_report_json(data, "health_diary", "2025-05-25", data_dir=output_dir)
    assert path.exists()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == "report.v1"


def test_write_report_markdown_direct(output_dir):
    data = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "health",
        "period_start": "2025-05-25",
        "period_end": "2025-05-25",
        "summary": "Test day.",
        "items": [],
        "gaps": [],
        "confidence": "low",
    }
    path = write_report_markdown(data, "health_diary", "2025-05-25", data_dir=output_dir)
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "2025-05-25" in content


# ── Test: Narrative ────────────────────────────────────────────────────


def test_narrative_with_data():
    metrics = {"sleep_hours": 7.5, "steps": 8000, "mood_score": 7}
    narrative = _build_narrative(metrics, [])
    assert "7.5h" in narrative
    assert "8,000 steps" in narrative or "8000 steps" in narrative
    assert "7.0/10" in narrative


def test_narrative_empty():
    narrative = _build_narrative({}, [])
    assert "No health metrics available" in narrative


def test_narrative_includes_flags():
    metrics = {"sleep_hours": 4.0}
    flags = _evaluate_flags(metrics)
    narrative = _build_narrative(metrics, flags)
    assert "low_sleep" in narrative


# ── Test: No temp files left behind ────────────────────────────────────


def test_no_tmp_files_left(rich_metrics, output_dir):
    """Atomic writes should not leave .tmp files."""
    report = HealthDiaryReport(rich_metrics, report_date="2025-05-25")
    report.write_json(data_dir=output_dir)
    report.write_markdown(data_dir=output_dir)
    tmp_files = list(output_dir.glob("*.tmp"))
    assert len(tmp_files) == 0


# ── Test: Date handling ────────────────────────────────────────────────


def test_date_as_date_object():
    report = HealthDiaryReport({"sleep_hours": 7}, report_date=date(2025, 5, 25))
    data = report.build()
    assert data["period_start"] == "2025-05-25"


def test_date_defaults_to_today():
    report = HealthDiaryReport({"sleep_hours": 7})
    data = report.build()
    today_str = datetime.now(timezone.utc).date().isoformat()
    assert data["period_start"] == today_str