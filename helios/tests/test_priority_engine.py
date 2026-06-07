"""Tests for Priority Engine full pipeline."""

import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from helios.priority.engine import PriorityEngine
from helios.priority.config import PriorityConfig
from helios.state import HeliosDB


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)  # let HeliosDB create fresh
    db = HeliosDB(path)
    yield db
    db.close()
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def priority_cfg():
    return PriorityConfig.from_raw({
        "mode": "shadow",
        "enabled": True,
        "log_all_candidates": True,
        "export_debug": False,  # skip file writes in tests
        "sources": {"rules": True},
    })


class TestPriorityEngineFullPipeline:
    def test_empty_rule_hits(self, db, priority_cfg):
        engine = PriorityEngine(db, cfg=priority_cfg.to_dict(), preferences=None, health=None)
        result = engine.evaluate_tick(
            context={"home": {"master_bedroom_temp_c": 20}},
            rule_hits=[],
        )
        assert result.generated_count == 0
        assert result.scored_count == 0
        assert result.mode == "shadow"
        assert result.error is None

    def test_single_rule_hit_pipeline(self, db, priority_cfg):
        engine = PriorityEngine(db, cfg=priority_cfg.to_dict(), preferences=None, health=None)
        rule_hits = [{
            "slug": "home_test",
            "title": "Test Alert",
            "message": "Something happened",
            "severity": "warning",
            "category": "home",
            "priority": 2,
            "module": "home",
        }]
        result = engine.evaluate_tick(
            context={"home": {"master_bedroom_temp_c": 22}},
            rule_hits=rule_hits,
        )
        assert result.generated_count == 1
        assert result.scored_count == 1
        assert result.selected_count == 1
        assert result.error is None

        # Verify DB persistence
        rows = db._execute("SELECT COUNT(*) FROM priority_candidates").fetchone()
        assert rows[0] == 1
        rows = db._execute("SELECT COUNT(*) FROM priority_scores").fetchone()
        assert rows[0] == 1
        rows = db._execute("SELECT COUNT(*) FROM priority_decisions").fetchone()
        assert rows[0] == 1

    def test_critical_alert_not_suppressed(self, db, priority_cfg):
        engine = PriorityEngine(db, cfg=priority_cfg.to_dict(), preferences=None, health=None)
        rule_hits = [{
            "slug": "safety_critical",
            "title": "CRITICAL SAFETY ALERT",
            "severity": "critical",
            "category": "health",
            "priority": 5,
        }]
        result = engine.evaluate_tick(
            context={},
            rule_hits=rule_hits,
        )
        assert result.generated_count == 1
        decisions = result.decisions
        assert decisions[0].decision.startswith("select_")
        assert "Critical" in decisions[0].reason or "critical" in decisions[0].reason

    def test_disabled_mode(self, db):
        cfg = PriorityConfig.from_raw({"enabled": False})
        engine = PriorityEngine(db, cfg=cfg.to_dict(), preferences=None, health=None)
        result = engine.evaluate_tick(context={}, rule_hits=[])
        assert result.generated_count == 0
        assert result.summary["status"] == "disabled"

    def test_multiple_candidates_ranked(self, db, priority_cfg):
        engine = PriorityEngine(db, cfg=priority_cfg.to_dict(), preferences=None, health=None)
        rule_hits = [
            {"slug": "info_rule", "title": "Info thing", "severity": "info", "category": "home", "priority": 1},
            {"slug": "warning_rule", "title": "Warning thing", "severity": "warning", "category": "system", "priority": 2},
            {"slug": "critical_rule", "title": "CRITICAL", "severity": "critical", "category": "health", "priority": 5},
        ]
        result = engine.evaluate_tick(context={}, rule_hits=rule_hits)
        assert result.generated_count == 3
        assert result.scored_count == 3

    def test_filter_removes_disabled_source(self, db, priority_cfg):
        cfg_disabled = PriorityConfig.from_raw({
            "enabled": True,
            "export_debug": False,
            "sources": {"rules": False},
        })
        engine = PriorityEngine(db, cfg=cfg_disabled.to_dict(), preferences=None, health=None)
        result = engine.evaluate_tick(context={}, rule_hits=[{
            "slug": "x",
            "title": "X",
            "severity": "info",
        }])
        assert result.generated_count == 0
        assert result.filtered_count == 0

    def test_db_schema_tables_exist(self, db):
        """Verify migration created priority tables."""
        tables = db._execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'priority_%'"
        ).fetchall()
        names = [r[0] for r in tables]
        assert "priority_candidates" in names
        assert "priority_scores" in names
        assert "priority_decisions" in names
        assert "priority_feedback" in names


class TestCandidateBuilderOnly:
    def test_rule_hit_source_generates_candidates(self):
        from helios.priority.builder import RuleHitCandidateSource
        src = RuleHitCandidateSource()
        cands = src.generate("tick-1", {}, [{"slug": "a", "title": "A"}], [])
        assert len(cands) == 1
        assert cands[0].rule_slug == "a"
        assert cands[0].candidate_type == "rule_alert"

    def test_invalid_hit_skipped(self):
        from helios.priority.builder import RuleHitCandidateSource
        src = RuleHitCandidateSource()
        cands = src.generate("tick-1", {}, ["not a dict"], [])
        assert len(cands) == 0
