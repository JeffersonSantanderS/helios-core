"""Tests for Candidate fingerprint stability across ticks."""

import pytest
from helios.priority.models import Candidate


class TestFingerprintStability:
    def test_same_rule_slug_same_fingerprint(self):
        fp1 = Candidate.make_fingerprint(
            candidate_type="rule_alert", source="rules_v2",
            rule_slug="energy_waste_empty_lights", module="home",
            title="Any title", raw_payload={"extra": 1},
        )
        fp2 = Candidate.make_fingerprint(
            candidate_type="rule_alert", source="rules_v2",
            rule_slug="energy_waste_empty_lights", module="home",
            title="Different title", raw_payload={"extra": 999},
        )
        assert fp1 == fp2  # slug dominates; payload extras ignored
        assert fp1 is not None
        assert len(fp1) == 16  # truncated sha256

    def test_different_rule_slug_different_fingerprint(self):
        fp1 = Candidate.make_fingerprint(
            candidate_type="rule_alert", source="rules_v2",
            rule_slug="rule_a", module="home",
        )
        fp2 = Candidate.make_fingerprint(
            candidate_type="rule_alert", source="rules_v2",
            rule_slug="rule_b", module="home",
        )
        assert fp1 != fp2

    def test_home_sensor_room_stable(self):
        fp1 = Candidate.make_fingerprint(
            candidate_type="home_environment_alert", source="home_sensor",
            module="home", title="room_temp_high",
            raw_payload={"room": "master_bedroom", "threshold": 27, "temp_c": 28.5},
        )
        fp2 = Candidate.make_fingerprint(
            candidate_type="home_environment_alert", source="home_sensor",
            module="home", title="room_temp_high",
            raw_payload={"room": "master_bedroom", "threshold": 27, "temp_c": 29.0},
        )
        assert fp1 == fp2  # temp_c changes don't affect fingerprint

    def test_different_room_different_fingerprint(self):
        fp1 = Candidate.make_fingerprint(
            candidate_type="home_environment_alert", source="home_sensor",
            module="home", title="room_temp_high",
            raw_payload={"room": "master_bedroom", "threshold": 27},
        )
        fp2 = Candidate.make_fingerprint(
            candidate_type="home_environment_alert", source="home_sensor",
            module="home", title="room_temp_high",
            raw_payload={"room": "spare_bedroom", "threshold": 27},
        )
        assert fp1 != fp2

    def test_module_health_stable(self):
        fp1 = Candidate.make_fingerprint(
            candidate_type="module_health_alert", source="module_health",
            module="health-api", title="module_failed",
            raw_payload={"state": "failed", "consecutive_failures": 5},
        )
        fp2 = Candidate.make_fingerprint(
            candidate_type="module_health_alert", source="module_health",
            module="health-api", title="module_failed",
            raw_payload={"state": "failed", "consecutive_failures": 6},
        )
        assert fp1 == fp2  # failure count changes don't affect fingerprint

    def test_no_slug_no_module_uses_title(self):
        fp1 = Candidate.make_fingerprint(
            candidate_type="generic", source="test",
            title="Something happened", raw_payload={},
        )
        fp2 = Candidate.make_fingerprint(
            candidate_type="generic", source="test",
            title="Something happened", raw_payload={"x": 1},
        )
        assert fp1 == fp2  # title dominates when no slug/module

    def test_candidate_includes_fingerprint(self):
        c = Candidate(
            candidate_id="c1", tick_id="t1", created_at="now",
            source="rules_v2", candidate_type="rule_alert",
            title="T", severity="info", category="system",
            fingerprint="abc123",
        )
        assert c.fingerprint == "abc123"
        d = c.to_dict()
        assert d["fingerprint"] == "abc123"
        c2 = Candidate.from_dict(d)
        assert c2.fingerprint == "abc123"
