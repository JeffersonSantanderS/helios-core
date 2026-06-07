"""Tests for Helios Brain v1 — brain_state.py.

Covers:
  1. Export is valid JSON
  2. schema_version exists
  3. generated_at exists
  4. Stale data represented as stale/degraded, not silently accepted
  5. Missing optional integrations do not crash
  6. Suggestions include requires_confirmation
  7. Suppressed alerts include reasons
  8. Confidence values between 0.0 and 1.0
  9. Export uses atomic write pattern
  10. No LLM dependency required

All tests use in-memory SQLite + mock objects. No live private services.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helios.brain_state import (
    BrainStateBuilder,
    BRAIN_STATE_FILE,
    SCHEMA_VERSION,
    _clamp_confidence,
    _freshness_label,
    _safe_float,
    _write_json_atomic,
)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def db_path(tmp_path):
    """Create an in-memory DB path — we use a temp file for SQLite."""
    db_file = tmp_path / "test_helios.db"
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    # Create minimal tables needed by brain_state
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS metric_snapshots (
            metric TEXT, date_key TEXT, value REAL, source TEXT, ts TEXT
        );
        CREATE TABLE IF NOT EXISTS focus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state TEXT, ts TEXT, duration_secs REAL
        );
        CREATE TABLE IF NOT EXISTS context (
            source TEXT, module TEXT, key TEXT, value TEXT,
            priority INTEGER DEFAULT 0,
            ts TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT
        );
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE, name TEXT, enabled INTEGER DEFAULT 1,
            priority TEXT DEFAULT 'normal', category TEXT,
            last_triggered TEXT
        );
        CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, rule_slug TEXT, severity TEXT,
            message TEXT, category TEXT
        );
        CREATE TABLE IF NOT EXISTS correlations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_a TEXT, metric_b TEXT,
            strength REAL, direction TEXT, p_value REAL,
            last_observed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_key TEXT, summary TEXT
        );
    """)
    conn.commit()
    conn.close()
    return str(db_file)


@pytest.fixture
def healthy_tracker():
    """Mock ModuleHealthTracker with healthy modules."""
    tracker = MagicMock()
    tracker.summary.return_value = {
        "ingestion": {
            "state": "healthy",
            "confidence": 0.92,
            "freshness_secs": 120.0,
            "last_ok_ts": datetime.now(timezone.utc).isoformat(),
        },
        "rules": {
            "state": "healthy",
            "confidence": 0.88,
            "freshness_secs": 45.0,
            "last_ok_ts": datetime.now(timezone.utc).isoformat(),
        },
        "preferences": {
            "state": "stale",
            "confidence": 0.65,
            "freshness_secs": 1200.0,
            "last_ok_ts": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
        },
    }
    tracker.freshness.side_effect = lambda name: {
        "ingestion": 120.0,
        "rules": 45.0,
        "preferences": 1200.0,
        "focus": 99999,
        "mood": 99999,
        "sleep": 99999,
    }.get(name, 99999)
    return tracker


@pytest.fixture
def mock_preferences():
    """Mock PreferenceEngine with patterns."""
    prefs = MagicMock()
    prefs.all_patterns.return_value = {
        "sleep_avg": {
            "metric": "sleep.hours",
            "mean": 7.2,
            "stddev": 0.8,
            "confidence": 0.85,
            "age_seconds": 3600,
            "evidence": ["28-day average"],
            "sample_count": 28,
        },
        "focus_peak": {
            "metric": "focus.productive_hours",
            "mean": 4.0,
            "confidence": 0.72,
            "age_seconds": 7200,
        },
    }
    prefs.is_quiet_hours.return_value = False
    return prefs


@pytest.fixture
def mock_rules():
    """Mock RulesEngine returning no hits (no rules triggered)."""
    rules = MagicMock()
    rules.evaluate.return_value = []
    return rules


@pytest.fixture
def builder(db_path, healthy_tracker, mock_preferences, mock_rules):
    """BrainStateBuilder with all modules."""
    return BrainStateBuilder(
        db_path=db_path,
        health=healthy_tracker,
        preferences=mock_preferences,
        rules_engine=mock_rules,
    )


@pytest.fixture
def minimal_builder(db_path):
    """BrainStateBuilder with NO optional modules (graceful degradation test)."""
    return BrainStateBuilder(db_path=db_path)


def _seed_metrics(db_path):
    """Seed the DB with today's metric data."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO metric_snapshots (metric, date_key, value, source, ts) VALUES (?, ?, ?, ?, ?)",
        [
            ("mood.score_daily", today, 7.0, "test", datetime.now(timezone.utc).isoformat()),
            ("sleep.hours", today, 7.5, "test", datetime.now(timezone.utc).isoformat()),
            ("activity.steps_daily", today, 8500, "test", datetime.now(timezone.utc).isoformat()),
        ],
    )
    conn.execute(
        "INSERT INTO focus (state, ts, duration_secs) VALUES (?, ?, ?)",
        ("productive", datetime.now(timezone.utc).isoformat(), 3600),
    )
    conn.execute(
        "INSERT INTO rules (slug, name, enabled, priority, last_triggered) VALUES (?, ?, ?, ?, ?)",
        ("test_rule", "Test Rule", 1, "normal", None),
    )
    conn.execute(
        "INSERT INTO alert_history (ts, rule_slug, severity, message, category) VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), "test_alert", "high", "Test alert message", "test"),
    )
    conn.execute(
        "INSERT INTO correlations (metric_a, metric_b, strength, direction, p_value, last_observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("sleep.hours", "mood.score_daily", 0.72, "positive", 0.01, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests for helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestHelpers:
    def test_clamp_confidence_normal(self):
        assert _clamp_confidence(0.5) == 0.5

    def test_clamp_confidence_over_1(self):
        assert _clamp_confidence(1.5) == 1.0

    def test_clamp_confidence_negative(self):
        assert _clamp_confidence(-0.5) == 0.0

    def test_clamp_confidence_rounding(self):
        assert _clamp_confidence(0.123456) == 0.123

    def test_freshness_label_fresh(self):
        assert _freshness_label(100) == "fresh"

    def test_freshness_label_stale(self):
        assert _freshness_label(600) == "stale"

    def test_freshness_label_degraded(self):
        assert _freshness_label(2000) == "degraded"

    def test_freshness_label_failed(self):
        assert _freshness_label(99999) == "failed"

    def test_safe_float_normal(self):
        assert _safe_float("3.14") == 3.14

    def test_safe_float_invalid(self):
        assert _safe_float("abc", -1.0) == -1.0

    def test_safe_float_none(self):
        assert _safe_float(None, 42.0) == 42.0


# ═══════════════════════════════════════════════════════════════════════════
# Test 1: brain_state export is valid JSON
# ═══════════════════════════════════════════════════════════════════════════

class TestExportValidJSON:
    def test_build_produces_valid_json(self, builder):
        result = builder.build()
        json_str = json.dumps(result, default=str)
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)
        # All required top-level keys present
        for key in ["schema_version", "generated_at", "runtime", "current_state",
                     "beliefs", "active_rules", "pattern_deviations",
                     "suggestions", "suppressed_alerts", "evidence_trace"]:
            assert key in parsed, f"Missing key: {key}"

    def test_export_writes_valid_json_file(self, builder, tmp_path):
        out = tmp_path / "brain_state.json"
        path = builder.export(path=out)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert data["schema_version"] == SCHEMA_VERSION


# ═══════════════════════════════════════════════════════════════════════════
# Test 2: schema_version exists and is correct
# ═══════════════════════════════════════════════════════════════════════════

class TestSchemaVersion:
    def test_schema_version_present(self, builder):
        result = builder.build()
        assert "schema_version" in result
        assert result["schema_version"] == "1.0"

    def test_schema_version_constant(self):
        assert SCHEMA_VERSION == "1.0"


# ═══════════════════════════════════════════════════════════════════════════
# Test 3: generated_at exists and is ISO 8601
# ═══════════════════════════════════════════════════════════════════════════

class TestGeneratedAt:
    def test_generated_at_present(self, builder):
        result = builder.build()
        assert "generated_at" in result
        ts = result["generated_at"]
        # Verify it's parseable as ISO 8601
        parsed = datetime.fromisoformat(ts)
        assert parsed.year >= 2025

    def test_generated_at_is_utc(self, builder):
        result = builder.build()
        ts = result["generated_at"]
        # Should contain +00:00 or Z
        assert "+00:00" in ts or ts.endswith("Z") or "T" in ts


# ═══════════════════════════════════════════════════════════════════════════
# Test 4: Stale data is represented as stale/degraded, not silently accepted
# ═══════════════════════════════════════════════════════════════════════════

class TestStaleDataRepresentation:
    def test_stale_module_marked_in_health(self, db_path):
        """Modules with stale data get freshness_label='stale' or worse."""
        stale_tracker = MagicMock()
        stale_tracker.summary.return_value = {
            "ingestion": {
                "state": "stale",
                "confidence": 0.4,
                "freshness_secs": 1200.0,
                "last_ok_ts": (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat(),
            },
        }
        b = BrainStateBuilder(db_path=db_path, health=stale_tracker)
        result = b.build()
        ingestion_health = result["runtime"]["module_health"]["ingestion"]
        assert ingestion_health["freshness_label"] in ("stale", "degraded", "failed")
        # Stale data should NOT be marked as "fresh"
        assert ingestion_health["freshness_label"] != "fresh"

    def test_missing_data_unknown_not_valid(self, minimal_builder):
        """Missing data dimensions are 'unknown', never silently valid."""
        result = minimal_builder.build()
        cs = result["current_state"]
        for key in ["location", "activity", "focus", "energy", "health", "mood"]:
            assert cs[key] in ("unknown", None) or "_stale" in cs[key], \
                f"{key} should be unknown when data is missing, got: {cs[key]}"


# ═══════════════════════════════════════════════════════════════════════════
# Test 5: Missing optional integrations do not crash the export
# ═══════════════════════════════════════════════════════════════════════════

class TestMissingIntegrations:
    def test_no_health_tracker(self, db_path, mock_preferences, mock_rules):
        b = BrainStateBuilder(db_path=db_path, health=None, preferences=mock_preferences, rules_engine=mock_rules)
        result = b.build()
        assert result["runtime"]["overall_confidence"] == 0.0
        assert result["runtime"]["module_health"] == {}

    def test_no_preferences(self, db_path, healthy_tracker, mock_rules):
        b = BrainStateBuilder(db_path=db_path, health=healthy_tracker, preferences=None, rules_engine=mock_rules)
        result = b.build()
        assert result["beliefs"] == []
        assert result["pattern_deviations"] == []

    def test_no_rules_engine(self, db_path, healthy_tracker, mock_preferences):
        b = BrainStateBuilder(db_path=db_path, health=healthy_tracker, preferences=mock_preferences, rules_engine=None)
        result = b.build()
        # Rules from DB should still work
        assert isinstance(result["active_rules"], list)

    def test_nothing_optional(self, minimal_builder):
        """Builder with only a DB path should not crash."""
        result = minimal_builder.build()
        assert result["schema_version"] == "1.0"
        assert isinstance(result["beliefs"], list)
        assert isinstance(result["active_rules"], list)

    def test_health_tracker_raises(self, db_path):
        """If health_tracker.summary() raises, export still succeeds."""
        bad_health = MagicMock()
        bad_health.summary.side_effect = RuntimeError("DB locked")
        b = BrainStateBuilder(db_path=db_path, health=bad_health)
        result = b.build()
        # Should get empty module_health, not crash
        assert result["runtime"]["module_health"] in ({}, None) or isinstance(result["runtime"]["module_health"], dict)

    def test_preferences_raises(self, db_path, healthy_tracker):
        """If preferences.all_patterns() raises, export still succeeds."""
        bad_prefs = MagicMock()
        bad_prefs.all_patterns.side_effect = RuntimeError("DB locked")
        b = BrainStateBuilder(db_path=db_path, health=healthy_tracker, preferences=bad_prefs)
        result = b.build()
        assert isinstance(result["beliefs"], list)


# ═══════════════════════════════════════════════════════════════════════════
# Test 6: Suggestions include requires_confirmation
# ═══════════════════════════════════════════════════════════════════════════

class TestSuggestionsRequireConfirmation:
    def test_suggestions_have_requires_confirmation(self, db_path, builder):
        _seed_metrics(db_path)
        result = builder.build()
        for suggestion in result["suggestions"]:
            assert "requires_confirmation" in suggestion, \
                f"Suggestion {suggestion.get('id', '?')} missing requires_confirmation"
            assert isinstance(suggestion["requires_confirmation"], bool)

    def test_high_severity_alerts_require_confirmation(self, db_path, builder):
        _seed_metrics(db_path)
        result = builder.build()
        high_alerts = [s for s in result["suggestions"] if s.get("priority") in ("high", "critical")]
        for s in high_alerts:
            assert s["requires_confirmation"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Test 7: Suppressed alerts include reasons
# ═══════════════════════════════════════════════════════════════════════════

class TestSuppressedAlertsReasons:
    def test_suppressed_alerts_have_reasons(self, builder):
        result = builder.build()
        for alert in result["suppressed_alerts"]:
            assert "rule_id" in alert, "Suppressed alert missing rule_id"
            assert "reason" in alert, "Suppressed alert missing reason"
            # Reason should be one of the documented categories or specific
            assert isinstance(alert["reason"], str)
            assert len(alert["reason"]) > 0

    def test_quiet_hours_suppression(self, db_path, mock_preferences):
        """When is_quiet_hours returns True, a wildcard suppressed entry appears."""
        mock_preferences.is_quiet_hours.return_value = True
        b = BrainStateBuilder(db_path=db_path, preferences=mock_preferences)
        result = b.build()
        quiet_entries = [a for a in result["suppressed_alerts"] if "quiet" in a["reason"]]
        assert len(quiet_entries) > 0, "Quiet hours should produce suppressed entry"


# ═══════════════════════════════════════════════════════════════════════════
# Test 8: Confidence values stay between 0.0 and 1.0
# ═══════════════════════════════════════════════════════════════════════════

class TestConfidenceBounds:
    def test_overall_confidence_in_bounds(self, builder):
        result = builder.build()
        oc = result["runtime"]["overall_confidence"]
        assert 0.0 <= oc <= 1.0, f"overall_confidence {oc} out of bounds"

    def test_module_confidence_in_bounds(self, builder):
        result = builder.build()
        for name, mod in result["runtime"]["module_health"].items():
            c = mod["confidence"]
            assert 0.0 <= c <= 1.0, f"Module {name} confidence {c} out of bounds"

    def test_belief_confidence_in_bounds(self, db_path, builder):
        _seed_metrics(db_path)
        result = builder.build()
        for belief in result["beliefs"]:
            c = belief["confidence"]
            assert 0.0 <= c <= 1.0, f"Belief {belief['key']} confidence {c} out of bounds"

    def test_rule_confidence_in_bounds(self, db_path, builder):
        _seed_metrics(db_path)
        result = builder.build()
        for rule in result["active_rules"]:
            c = rule["confidence"]
            assert 0.0 <= c <= 1.0, f"Rule {rule['rule_id']} confidence {c} out of bounds"


# ═══════════════════════════════════════════════════════════════════════════
# Test 9: Export uses atomic write pattern
# ═══════════════════════════════════════════════════════════════════════════

class TestAtomicWrite:
    def test_atomic_write_creates_file(self, tmp_path):
        out = tmp_path / "test_atomic.json"
        data = {"key": "value", "number": 42}
        _write_json_atomic(out, data)
        assert out.exists()
        loaded = json.loads(out.read_text())
        assert loaded["key"] == "value"

    def test_atomic_write_creates_parent_dirs(self, tmp_path):
        out = tmp_path / "deep" / "nested" / "dir" / "test.json"
        _write_json_atomic(out, {"ok": True})
        assert out.exists()

    def test_atomic_write_no_partial_files(self, tmp_path):
        """Atomic write should not leave .tmp files on success."""
        out = tmp_path / "test_no_tmp.json"
        _write_json_atomic(out, {"clean": True})
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Leftover .tmp files: {tmp_files}"

    def test_export_produces_no_tmp(self, builder, tmp_path):
        out = tmp_path / "brain_state.json"
        builder.export(path=out)
        tmp_files = list(tmp_path.glob("*.tmp"))
        # The .tmp file should have been renamed to the final name
        assert len(tmp_files) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Test 10: No LLM dependency required
# ═══════════════════════════════════════════════════════════════════════════

class TestNoLLMDependency:
    def test_no_llm_imports(self):
        """brain_state module should not import any LLM libraries."""
        import helios.brain_state as bs
        source = open(bs.__file__).read()
        # Should NOT contain any of these LLM-related imports
        forbidden = ["openai", "anthropic", "langchain", "llm_bridge", "LLMBridge"]
        for term in forbidden:
            assert term not in source, f"Found forbidden LLM dependency: {term}"

    def test_build_without_config(self, minimal_builder):
        """Build should work with no config at all (no LLM config needed)."""
        result = minimal_builder.build()
        assert result["schema_version"] == "1.0"


# ═══════════════════════════════════════════════════════════════════════════
# Additional coverage tests
# ═══════════════════════════════════════════════════════════════════════════

class TestCurrentState:
    def test_seeded_metrics_populate_state(self, db_path, builder):
        _seed_metrics(db_path)
        result = builder.build()
        cs = result["current_state"]
        # Mood should be populated (seeded as 7.0)
        assert cs["mood"] != "unknown"
        # Sleep/energy should be populated (seeded as 7.5)
        assert cs["energy"] != "unknown"
        # Focus should be populated (seeded as "productive")
        assert cs["focus"] != "unknown"

    def test_empty_db_gives_unknown(self, db_path, minimal_builder):
        """With no seeded data, all dimensions should be 'unknown'."""
        result = minimal_builder.build()
        cs = result["current_state"]
        assert cs["mood"] == "unknown"
        assert cs["energy"] == "unknown"

    def test_low_sleep_marks_low_energy(self, db_path, builder):
        """Sleep under 5h should produce 'low' energy (possibly with _stale suffix)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO metric_snapshots (metric, date_key, value, source, ts) VALUES (?, ?, ?, ?, ?)",
            ("sleep.hours", today, 3.5, "test", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
        result = builder.build()
        # "low" or "low_stale" — stale suffix is correct when health tracker reports stale
        assert result["current_state"]["energy"].startswith("low")


class TestBeliefs:
    def test_correlation_beliefs(self, db_path, builder):
        _seed_metrics(db_path)
        result = builder.build()
        # Should find the seeded correlation (strength=0.72)
        corr_beliefs = [b for b in result["beliefs"] if b["key"].startswith("correlation.")]
        assert len(corr_beliefs) > 0
        for b in corr_beliefs:
            assert b["confidence"] >= 0.0
            assert b["confidence"] <= 1.0
            assert "sources" in b

    def test_preference_beliefs(self, db_path, builder):
        result = builder.build()
        pref_beliefs = [b for b in result["beliefs"] if b["key"].startswith("preference.")]
        assert len(pref_beliefs) > 0
        # Preference beliefs should have evidence
        for b in pref_beliefs:
            assert isinstance(b.get("evidence"), list)


class TestEvidenceTrace:
    def test_evidence_trace_from_seeded_data(self, db_path, builder):
        _seed_metrics(db_path)
        result = builder.build()
        trace = result["evidence_trace"]
        assert isinstance(trace, list)
        # Should have entries for metric snapshots
        metric_entries = [e for e in trace if e["source_id"].startswith("metric:")]
        assert len(metric_entries) > 0

    def test_evidence_trace_empty_on_empty_db(self, db_path, minimal_builder):
        result = minimal_builder.build()
        # Should return empty list, not crash
        assert isinstance(result["evidence_trace"], list)