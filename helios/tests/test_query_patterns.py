"""Tests for helios.query_patterns — Personal Query Answer Contract."""

import pytest
from datetime import date

from helios.query_patterns import (
    QueryAnswer,
    QUERY_PATTERNS,
    answer_query,
    handle_where_was_i,
    handle_when_leave_work,
    handle_hours_worked,
    handle_sleep,
    handle_weekly_change,
    handle_needs_review,
    handle_why_alerted,
)


# ── QueryAnswer structure tests ────────────────────────────────────────────


class TestQueryAnswerStructure:
    """Verify QueryAnswer dataclass and field constraints."""

    def test_create_query_answer(self):
        """QueryAnswer can be created with all fields."""
        qa = QueryAnswer(
            answer="Near home.",
            confidence="high",
            evidence=["location source=home_assistant"],
            gaps=[],
            privacy_level="safe_for_user_dm",
        )
        assert qa.answer == "Near home."
        assert qa.confidence == "high"
        assert len(qa.evidence) == 1
        assert qa.gaps == []
        assert qa.privacy_level == "safe_for_user_dm"

    def test_default_privacy_level(self):
        """privacy_level defaults to safe_for_user_dm."""
        qa = QueryAnswer(
            answer="Test",
            confidence="medium",
        )
        assert qa.privacy_level == "safe_for_user_dm"

    def test_confidence_values(self):
        """All four confidence levels are valid."""
        for level in ("high", "medium", "low", "needs_review"):
            qa = QueryAnswer(answer="Test", confidence=level)
            assert qa.confidence == level

    def test_evidence_and_gaps_defaults(self):
        """evidence and gaps default to empty lists."""
        qa = QueryAnswer(answer="Test", confidence="low")
        assert qa.evidence == []
        assert qa.gaps == []


# ── Pattern registry tests ──────────────────────────────────────────────────


class TestQueryPatternsRegistry:
    """Verify QUERY_PATTERNS contains all expected entries."""

    def test_all_patterns_registered(self):
        """All 7 query patterns are registered."""
        expected = {
            "where_was_i_today",
            "when_did_i_leave_work",
            "how_many_hours_worked",
            "how_did_i_sleep",
            "what_changed_this_week",
            "what_needs_review",
            "why_alerted",
        }
        assert set(QUERY_PATTERNS.keys()) == expected

    def test_pattern_handlers_are_callable(self):
        """All pattern handlers are callable."""
        for name, handler in QUERY_PATTERNS.items():
            assert callable(handler), f"Handler for {name} is not callable"

    def test_answer_query_dispatch(self):
        """answer_query dispatches to the right handler."""
        result = answer_query("how_did_i_sleep", health_data=None)
        assert isinstance(result, QueryAnswer)
        assert result.confidence == "needs_review"

    def test_answer_query_invalid_pattern(self):
        """answer_query raises KeyError for unknown pattern."""
        with pytest.raises(KeyError):
            answer_query("nonexistent_pattern")


# ── Missing data returns needs_review ───────────────────────────────────────


class TestMissingData:
    """All handlers return needs_review with explicit gaps when data is missing."""

    def test_where_was_i_missing_data(self):
        result = handle_where_was_i(location_data=None)
        assert result.confidence == "needs_review"
        assert len(result.gaps) > 0
        assert "data" in result.gaps[0].lower() or "location" in result.gaps[0].lower()

    def test_when_leave_work_missing_data(self):
        result = handle_when_leave_work(
            work_hours_data=None, location_data=None
        )
        assert result.confidence == "needs_review"
        assert len(result.gaps) > 0

    def test_hours_worked_missing_data(self):
        result = handle_hours_worked(work_hours_data=None)
        assert result.confidence == "needs_review"
        assert len(result.gaps) > 0

    def test_sleep_missing_data(self):
        result = handle_sleep(health_data=None)
        assert result.confidence == "needs_review"
        assert len(result.gaps) > 0

    def test_weekly_change_missing_all_data(self):
        result = handle_weekly_change(
            work_hours_data=None,
            health_data=None,
            location_data=None,
            spotify_data=None,
        )
        assert result.confidence == "needs_review"
        assert len(result.gaps) > 0

    def test_needs_review_missing_data(self):
        result = handle_needs_review(work_hours_data=None)
        assert result.confidence == "needs_review"
        assert len(result.gaps) > 0

    def test_why_alerted_missing_data(self):
        result = handle_why_alerted(
            module_staleness_data=None,
            location_data=None,
            health_data=None,
        )
        # Should still produce a valid answer
        assert isinstance(result, QueryAnswer)


# ── Raw coordinates excluded ────────────────────────────────────────────────


class TestNoRawCoordinates:
    """Raw coordinates must never appear in answers."""

    def test_where_was_i_no_coords(self):
        """Location answer must not contain raw lat/lon."""
        loc_data = {
            "zone_label": "home",
            "city": "Anytown",
            "lat": 40.7128,
            "lon": -74.0060,
            "freshness_secs": 60,
            "source": "home_assistant",
        }
        result = handle_where_was_i(location_data=loc_data)
        answer_lower = result.answer.lower()
        # No raw decimal coordinates in the answer
        assert "40.7128" not in answer_lower
        assert "-74.0060" not in answer_lower
        # No raw coordinates in evidence
        for e in result.evidence:
            assert "40.7128" not in e
            assert "-74.0060" not in e

    def test_why_alerted_no_coords(self):
        """Alert answer must not leak coordinates from location data."""
        loc_data = {
            "zone_label": "away",
            "freshness_secs": 7200,
            "lat": 40.7128,
            "lon": -74.0060,
        }
        result = handle_why_alerted(location_data=loc_data)
        result_text = result.answer + " ".join(result.evidence)
        assert "40.7128" not in result_text
        assert "-74.0060" not in result_text


# ── Evidence cites data sources ──────────────────────────────────────────────


class TestEvidenceCitations:
    """Evidence lists must cite data sources."""

    def test_where_was_i_evidence(self):
        loc_data = {
            "zone_label": "home",
            "freshness_secs": 30,
            "source": "home_assistant",
            "last_updated": "2026-05-24T12:00:00Z",
        }
        result = handle_where_was_i(location_data=loc_data)
        assert result.confidence == "high"
        assert any("source" in e for e in result.evidence)
        assert any("home_assistant" in e for e in result.evidence)

    def test_hours_worked_evidence(self):
        work_data = {
            "current_pay_period": "May 11 - May 22",
            "days": [
                {"date": "2026-05-11", "kind": "work", "paid_hours": 8.0,
                 "confidence": "high", "note": "", "source": "location_inference"},
            ],
            "confidence_counts": {"high": 1},
        }
        result = handle_hours_worked(work_hours_data=work_data)
        assert len(result.evidence) > 0
        assert any("work_hours" in e for e in result.evidence)

    def test_sleep_evidence(self):
        health_data = {
            "sleep_hours": 7.5,
            "confidence": "high",
            "stale_data_warnings": [],
        }
        result = handle_sleep(health_data=health_data)
        assert len(result.evidence) > 0
        assert any("sleep" in e.lower() for e in result.evidence)

    def test_needs_review_evidence(self):
        work_data = {
            "needs_review": [
                {"date": "2026-05-15", "reason": "no away data"},
            ],
        }
        result = handle_needs_review(work_hours_data=work_data)
        assert len(result.evidence) > 0

    def test_why_alerted_evidence(self):
        staleness_data = {
            "stale_modules": ["location", "calendar"],
            "modules": [
                {"module_name": "location", "freshness_secs": 7200, "confidence": 0.5, "state": "healthy"},
                {"module_name": "calendar", "freshness_secs": 3600, "confidence": 0.8, "state": "healthy"},
            ],
        }
        result = handle_why_alerted(module_staleness_data=staleness_data)
        assert len(result.evidence) > 0
        assert any("stale" in e.lower() for e in result.evidence)


# ── Confidence honesty ──────────────────────────────────────────────────────


class TestConfidenceHonesty:
    """Confidence levels must be honest about data quality."""

    def test_location_high_confidence_when_fresh(self):
        loc_data = {
            "zone_label": "home",
            "freshness_secs": 60,  # very fresh
            "source": "home_assistant",
        }
        result = handle_where_was_i(location_data=loc_data)
        assert result.confidence == "high"

    def test_location_medium_confidence_when_staleish(self):
        loc_data = {
            "zone_label": "home",
            "freshness_secs": 1800,  # 30 minutes
            "source": "home_assistant",
        }
        result = handle_where_was_i(location_data=loc_data)
        assert result.confidence == "medium"

    def test_location_low_confidence_when_old(self):
        loc_data = {
            "zone_label": "home",
            "freshness_secs": 7200,  # 2 hours
            "source": "home_assistant",
        }
        result = handle_where_was_i(location_data=loc_data)
        assert result.confidence == "low"

    def test_sleep_needs_review_without_data(self):
        health_data = {"confidence": "needs_review", "stale_data_warnings": []}
        result = handle_sleep(health_data=health_data)
        assert result.confidence == "needs_review"
        assert "sleep" in result.gaps[0].lower() or "missing" in result.gaps[0].lower()

    def test_hours_worked_needs_review_when_review_days(self):
        work_data = {
            "current_pay_period": "May 11 - May 22",
            "days": [
                {"date": "2026-05-11", "kind": "needs_review", "paid_hours": 0.0,
                 "confidence": "needs_review", "note": "no away data", "source": "needs_review"},
            ],
            "confidence_counts": {"needs_review": 1},
        }
        result = handle_hours_worked(work_hours_data=work_data)
        assert result.confidence == "needs_review"
        assert len(result.gaps) > 0


# ── Handler-specific logic tests ────────────────────────────────────────────


class TestHandleWhereWasI:
    """Detailed tests for handle_where_was_i."""

    def test_zone_label_precedence(self):
        """zone takes precedence over city in answer."""
        result = handle_where_was_i(location_data={
            "zone_label": "office",
            "city": "Anytown",
            "freshness_secs": 60,
        })
        assert "office" in result.answer

    def test_home_from_is_home(self):
        """is_home=True produces 'home' zone_label."""
        result = handle_where_was_i(location_data={
            "is_home": True,
            "city": "Anytown",
            "freshness_secs": 60,
        })
        assert "home" in result.answer.lower()

    def test_city_fallback(self):
        """City is used when zone is 'away' and is_home is False."""
        result = handle_where_was_i(location_data={
            "city": "Edmonton",
            "zone": "away",
            "freshness_secs": 300,
        })
        assert "Edmonton" in result.answer


class TestHandleHoursWorked:
    """Detailed tests for handle_hours_worked."""

    def test_total_hours_calculation(self):
        work_data = {
            "current_pay_period": "May 11 - May 22",
            "days": [
                {"date": "2026-05-11", "kind": "work", "paid_hours": 8.0,
                 "confidence": "high", "note": "", "source": "location_inference"},
                {"date": "2026-05-12", "kind": "work", "paid_hours": 7.5,
                 "confidence": "high", "note": "", "source": "location_inference"},
            ],
            "confidence_counts": {"high": 2},
        }
        result = handle_hours_worked(work_hours_data=work_data)
        assert "15.5h" in result.answer or "15h" in result.answer or "15" in result.answer
        assert result.confidence == "high"

    def test_empty_days_no_crash(self):
        work_data = {"days": [], "current_pay_period": "Test"}
        result = handle_hours_worked(work_hours_data=work_data)
        assert result.confidence == "needs_review"


class TestHandleSleep:
    """Detailed tests for handle_sleep."""

    def test_low_sleep_description(self):
        result = handle_sleep(health_data={
            "sleep_hours": 4.0,
            "confidence": "medium",
        })
        assert "4" in result.answer
        assert "little" in result.answer.lower() or "less" in result.answer.lower()

    def test_normal_sleep_description(self):
        result = handle_sleep(health_data={
            "sleep_hours": 7.5,
            "confidence": "high",
        })
        assert "7" in result.answer

    def test_stale_warnings_in_gaps(self):
        result = handle_sleep(health_data={
            "sleep_hours": 7.0,
            "confidence": "low",
            "stale_data_warnings": ["resting_hr missing", "steps missing"],
        })
        assert len(result.gaps) > 0


class TestHandleNeedsReview:
    """Detailed tests for handle_needs_review."""

    def test_nothing_needs_review(self):
        work_data = {
            "needs_review": [],
            "days": [
                {"date": "2026-05-11", "kind": "work", "confidence": "high"},
            ],
        }
        result = handle_needs_review(work_hours_data=work_data)
        assert result.confidence == "high"
        assert "nothing" in result.answer.lower() or "no" in result.answer.lower()

    def test_items_need_review(self):
        work_data = {
            "needs_review": [
                {"date": "2026-05-15", "reason": "no away data"},
                {"date": "2026-05-16", "reason": "transit-like cluster"},
            ],
        }
        result = handle_needs_review(work_hours_data=work_data)
        assert "2" in result.answer
        assert result.confidence == "medium"


class TestHandleWhyAlerted:
    """Detailed tests for handle_why_alerted."""

    def test_no_alerts(self):
        result = handle_why_alerted(
            module_staleness_data={"stale_modules": [], "modules": [
                {"module_name": "location", "freshness_secs": 5, "state": "healthy"},
            ]},
        )
        assert "nominal" in result.answer.lower() or "no alert" in result.answer.lower()

    def test_stale_modules_alert(self):
        result = handle_why_alerted(
            module_staleness_data={
                "stale_modules": ["location"],
                "modules": [
                    {"module_name": "location", "freshness_secs": 7200, "state": "healthy"},
                ],
            }
        )
        assert "location" in result.answer

    def test_stale_location_alert(self):
        result = handle_why_alerted(location_data={
            "freshness_secs": 7200,  # 2 hours old
        })
        assert "2h" in result.answer or "location" in result.answer.lower()


class TestHandleWeeklyChange:
    """Detailed tests for handle_weekly_change."""

    def test_combined_summary(self):
        result = handle_weekly_change(
            work_hours_data={"days": [
                {"kind": "work", "paid_hours": 8.0},
            ], "current_pay_period": "this week"},
            health_data={"sleep_hours": 7.5},
            location_data={"zone_label": "home"},
            spotify_data={"total_minutes": 45},
        )
        assert "worked" in result.answer.lower()
        assert "slept" in result.answer.lower()
        assert len(result.evidence) >= 2

    def test_partial_data(self):
        result = handle_weekly_change(
            health_data={"sleep_hours": 6.0},
        )
        assert "slept" in result.answer.lower()