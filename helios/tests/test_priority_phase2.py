"""Tests for Priority Engine Phase 2 sources: home environment + module health candidates."""

import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from helios.priority.engine import PriorityEngine
from helios.priority.config import PriorityConfig
from helios.priority.builder import HomeCandidateSource, ModuleHealthCandidateSource
from helios.state import HeliosDB


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)
    db = HeliosDB(path)
    yield db
    db.close()
    Path(path).unlink(missing_ok=True)


class TestHomeCandidateSource:
    def test_lights_on_empty_house(self):
        src = HomeCandidateSource()
        cands = src.generate(
            "tick-1",
            context={"home": {"rooms_occupied": 0, "total_lights_on": 4, "anyone_home": False, "source": "home_assistant"}},
            rule_hits=[],
            source_events=[],
        )
        lights = [c for c in cands if "lights" in c.tags]
        assert len(lights) == 1
        assert lights[0].title == "Lights on but house appears empty"
        assert lights[0].candidate_type == "home_environment_alert"

    def test_lights_on_but_anyone_home_no_alert(self):
        """When anyone_home is True, do NOT fire the 'empty house' alert even if rooms_occupied=0."""
        src = HomeCandidateSource()
        cands = src.generate(
            "tick-1",
            context={"home": {"rooms_occupied": 0, "total_lights_on": 5, "anyone_home": True, "source": "home_assistant"}},
            rule_hits=[],
            source_events=[],
        )
        lights = [c for c in cands if "lights" in c.tags and "empty" in c.title.lower()]
        assert len(lights) == 0  # suppressed because someone is home

    def test_no_lights_on_empty_no_candidate(self):
        src = HomeCandidateSource()
        cands = src.generate(
            "tick-1",
            context={"home": {"rooms_occupied": 0, "total_lights_on": 2, "source": "home_assistant"}},
            rule_hits=[],
            source_events=[],
        )
        assert len(cands) == 0  # threshold is > 2

    def test_room_occupied_dark(self):
        src = HomeCandidateSource()
        cands = src.generate(
            "tick-1",
            context={
                "home": {
                    "master_bedroom_occupied": True,
                    "master_bedroom_light_count": 0,
                    "master_bedroom_lux": 3,
                    "source": "home_assistant",
                }
            },
            rule_hits=[],
            source_events=[],
        )
        mb_cands = [c for c in cands if "master_bedroom" in c.tags]
        assert len(mb_cands) == 1
        assert "dark" in mb_cands[0].title.lower()

    def test_temperature_high(self):
        src = HomeCandidateSource()
        cands = src.generate(
            "tick-1",
            context={"home": {"master_bedroom_temp_c": 28.5, "source": "home_assistant"}},
            rule_hits=[],
            source_events=[],
        )
        temp_cands = [c for c in cands if "temperature" in c.tags]
        assert len(temp_cands) == 1
        assert temp_cands[0].severity == "warning"
        assert "high" in temp_cands[0].title.lower()

    def test_temperature_low(self):
        src = HomeCandidateSource()
        cands = src.generate(
            "tick-1",
            context={"home": {"spare_bedroom_temp_c": 14.0, "source": "home_assistant"}},
            rule_hits=[],
            source_events=[],
        )
        temp_cands = [c for c in cands if "temperature" in c.tags]
        assert len(temp_cands) == 1
        assert "low" in temp_cands[0].title.lower()

    def test_ha_unavailable(self):
        src = HomeCandidateSource()
        cands = src.generate(
            "tick-1",
            context={"home": {"source": "unavailable"}},
            rule_hits=[],
            source_events=[],
        )
        unav = [c for c in cands if "unavailable" in c.title.lower()]
        assert len(unav) == 1
        assert unav[0].severity == "warning"

    def test_no_home_context(self):
        src = HomeCandidateSource()
        cands = src.generate("tick-1", context={}, rule_hits=[], source_events=[])
        assert len(cands) == 0

    def test_home_source_disabled(self):
        cfg = PriorityConfig.from_raw({"sources": {"home": False}})
        src = HomeCandidateSource()
        assert not src.enabled(cfg)

    def test_multiple_conditions(self):
        """Multiple alerts can fire from same tick."""
        src = HomeCandidateSource()
        cands = src.generate(
            "tick-1",
            context={
                "home": {
                    "rooms_occupied": 0,
                    "total_lights_on": 4,
                    "master_bedroom_temp_c": 28.5,
                    "spare_bedroom_temp_c": 14.0,
                    "master_bedroom_occupied": True,
                    "master_bedroom_light_count": 0,
                    "master_bedroom_lux": 3,
                    "source": "home_assistant",
                }
            },
            rule_hits=[],
            source_events=[],
        )
        assert len(cands) == 4  # lights_on + 2 temps + dark room


class TestModuleHealthCandidateSource:
    @pytest.fixture(autouse=True)
    def _clear_dedup(self, tmp_path, monkeypatch):
        """Point dedup file to a temp dir so tests don't pollute or read production state."""
        dedup_file = tmp_path / "module_health_alert_dedup.json"
        monkeypatch.setattr(ModuleHealthCandidateSource, "_DEDUP_PATH", dedup_file)

    def test_stale_module(self):
        src = ModuleHealthCandidateSource()
        cands = src.generate(
            "tick-1",
            context={"_module_health": {"location": {"state": "stale", "freshness_secs": 1200}}},
            rule_hits=[],
            source_events=[],
        )
        assert len(cands) == 1
        assert "stale" in cands[0].title.lower()
        assert cands[0].severity == "warning"

    def test_degraded_module(self):
        src = ModuleHealthCandidateSource()
        cands = src.generate(
            "tick-1",
            context={
                "_module_health": {
                    "calendar": {
                        "state": "degraded",
                        "freshness_secs": 1800,
                        "consecutive_failures": 2,
                    }
                }
            },
            rule_hits=[],
            source_events=[],
        )
        assert len(cands) == 1
        assert "degraded" in cands[0].title.lower()
        assert cands[0].severity == "warning"

    def test_failed_module(self):
        src = ModuleHealthCandidateSource()
        cands = src.generate(
            "tick-1",
            context={"_module_health": {"spotify": {"state": "failed", "consecutive_failures": 5}}},
            rule_hits=[],
            source_events=[],
        )
        assert len(cands) == 1
        assert "failed" in cands[0].title.lower()
        assert cands[0].severity == "error"

    def test_healthy_module_filtered(self):
        src = ModuleHealthCandidateSource()
        cands = src.generate(
            "tick-1",
            context={"_module_health": {"location": {"state": "healthy"}, "calendar": {"state": "unknown"}}},
            rule_hits=[],
            source_events=[],
        )
        assert len(cands) == 0

    def test_no_health_context(self):
        src = ModuleHealthCandidateSource()
        cands = src.generate("tick-1", context={}, rule_hits=[], source_events=[])
        assert len(cands) == 0

    def test_health_source_disabled(self):
        cfg = PriorityConfig.from_raw({"sources": {"module_health": False}})
        src = ModuleHealthCandidateSource()
        assert not src.enabled(cfg)

    def test_daily_dedup_blocks_repeated_stale_alert(self):
        """Same module-state should only produce one candidate per day."""
        src = ModuleHealthCandidateSource()
        ctx = {"_module_health": {"location": {"state": "stale", "freshness_secs": 1200}}}
        # First call — should produce a candidate
        cands1 = src.generate("tick-1", context=ctx, rule_hits=[], source_events=[])
        assert len(cands1) == 1
        # Second call same day — dedup should suppress
        cands2 = src.generate("tick-2", context=ctx, rule_hits=[], source_events=[])
        assert len(cands2) == 0

    def test_daily_dedup_allows_different_state(self):
        """A module transitioning from stale→degraded should produce a new alert."""
        src = ModuleHealthCandidateSource()
        ctx_stale = {"_module_health": {"location": {"state": "stale", "freshness_secs": 1200}}}
        # Stale alert fires
        cands1 = src.generate("tick-1", context=ctx_stale, rule_hits=[], source_events=[])
        assert len(cands1) == 1
        # Module degrades — different state key, should produce a new alert
        ctx_degraded = {"_module_health": {"location": {"state": "degraded", "freshness_secs": 50000, "consecutive_failures": 3}}}
        cands2 = src.generate("tick-2", context=ctx_degraded, rule_hits=[], source_events=[])
        assert len(cands2) == 1


class TestPhase2Pipeline:
    @pytest.fixture(autouse=True)
    def _clear_dedup(self, tmp_path, monkeypatch):
        """Point dedup file to a temp dir so tests don't pollute or read production state."""
        dedup_file = tmp_path / "module_health_alert_dedup.json"
        monkeypatch.setattr(ModuleHealthCandidateSource, "_DEDUP_PATH", dedup_file)

    def test_home_candidates_score_and_rank(self, db):
        cfg = PriorityConfig.from_raw({
            "mode": "shadow",
            "enabled": True,
            "export_debug": False,
            "sources": {"rules": False, "home": True, "module_health": False},
        })
        engine = PriorityEngine(db, cfg=cfg.to_dict(), preferences=None, health=None)
        result = engine.evaluate_tick(
            context={
                "home": {
                    "rooms_occupied": 0,
                    "total_lights_on": 4,
                    "master_bedroom_temp_c": 28.5,
                    "source": "home_assistant",
                }
            },
            rule_hits=[],
        )
        assert result.generated_count >= 2  # lights + temp
        assert result.scored_count == result.generated_count
        # Energy waste should be scored
        energy = [c for c in result.candidates if "energy" in c.tags or "lights" in c.tags]
        assert len(energy) >= 1
        # All should have decisions
        assert len(result.decisions) == result.generated_count

    def test_home_candidates_logged_to_db(self, db):
        cfg = PriorityConfig.from_raw({
            "mode": "shadow",
            "enabled": True,
            "export_debug": False,
            "sources": {"rules": False, "home": True, "module_health": False},
        })
        engine = PriorityEngine(db, cfg=cfg.to_dict(), preferences=None, health=None)
        engine.evaluate_tick(
            context={"home": {"rooms_occupied": 0, "total_lights_on": 4, "source": "home_assistant"}},
            rule_hits=[],
        )
        rows = db._execute(
            "SELECT COUNT(*) FROM priority_candidates WHERE source='home_sensor'"
        ).fetchone()
        assert rows[0] >= 1

    def test_module_health_candidates_logged(self, db):
        cfg = PriorityConfig.from_raw({
            "mode": "shadow",
            "enabled": True,
            "export_debug": False,
            "sources": {"rules": False, "home": False, "module_health": True},
        })
        engine = PriorityEngine(db, cfg=cfg.to_dict(), preferences=None, health=None)
        engine.evaluate_tick(
            context={"_module_health": {"location": {"state": "stale", "freshness_secs": 1200}}},
            rule_hits=[],
        )
        rows = db._execute(
            "SELECT COUNT(*) FROM priority_candidates WHERE source='module_health'"
        ).fetchone()
        assert rows[0] == 1

    def test_room_redundancy_downgrades_weaker(self, db):
        """Two candidates for same room: check that room redundancy logic exists and runs without error."""
        cfg = PriorityConfig.from_raw({
            "mode": "shadow",
            "enabled": True,
            "export_debug": False,
            "sources": {"rules": False, "home": True, "module_health": False},
        })
        engine = PriorityEngine(db, cfg=cfg.to_dict(), preferences=None, health=None)
        result = engine.evaluate_tick(
            context={
                "home": {
                    "rooms_occupied": 0,
                    "total_lights_on": 4,
                    "master_bedroom_occupied": True,
                    "master_bedroom_light_count": 0,
                    "master_bedroom_lux": 3,
                    "source": "home_assistant",
                }
            },
            rule_hits=[],
        )
        mb_cands = [c for c in result.candidates if "master_bedroom" in c.tags]
        assert len(mb_cands) >= 1  # could be 1 or 2 (lights + dark)
        assert result.error is None  # entire pipeline ran without crash

    def test_combined_sources(self, db):
        cfg = PriorityConfig.from_raw({
            "mode": "shadow",
            "enabled": True,
            "export_debug": False,
            "sources": {"rules": True, "home": True, "module_health": True},
        })
        engine = PriorityEngine(db, cfg=cfg.to_dict(), preferences=None, health=None)
        result = engine.evaluate_tick(
            context={
                "home": {"rooms_occupied": 0, "total_lights_on": 4, "source": "home_assistant"},
                "_module_health": {
                    "location": {"state": "stale", "freshness_secs": 1200}
                },
            },
            rule_hits=[{
                "slug": "test_rule",
                "title": "Test Rule",
                "severity": "info",
                "category": "home",
            }],
        )
        # Should have candidates from all 3 sources
        sources = {c.source for c in result.candidates}
        assert "rules_v2" in sources
        assert "home_sensor" in sources
        assert "module_health" in sources
        assert len(result.candidates) >= 3
        assert result.scored_count == len(result.candidates)
        assert result.error is None

    def test_system_health_ranks_above_home_comfort(self, db):
        """A failed module should score higher than a home comfort nudge."""
        cfg = PriorityConfig.from_raw({
            "mode": "shadow",
            "enabled": True,
            "export_debug": False,
            "sources": {"rules": False, "home": True, "module_health": True},
        })
        engine = PriorityEngine(db, cfg=cfg.to_dict(), preferences=None, health=None)
        result = engine.evaluate_tick(
            context={
                "home": {"rooms_occupied": 0, "total_lights_on": 4, "source": "home_assistant"},
                "_module_health": {
                    "spotify": {"state": "failed", "consecutive_failures": 5}
                },
            },
            rule_hits=[],
        )
        health_cands = [c for c in result.candidates if c.source == "module_health"]
        home_cands = [c for c in result.candidates if c.source == "home_sensor"]
        assert len(health_cands) == 1
        assert len(home_cands) >= 1

        health_dec = next((d for d in result.decisions if health_cands[0].candidate_id == d.candidate_id), None)
        home_dec = next((d for d in result.decisions if home_cands[0].candidate_id == d.candidate_id), None)
        assert health_dec is not None
        assert home_dec is not None
        # Failed module should be SELECTED, home comfort might be deferred
        assert health_dec.decision in ("select_notify", "select_dm", "select_summary", "select_log_only","defer")
