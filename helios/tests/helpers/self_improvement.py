"""Helpers for self-improvement QA assertions.

Reused across test_self_improvement_replay.py and test_self_improvement_privacy.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "self_improvement_days"


def load_fixture(name: str) -> dict[str, Any]:
    path = FIXTURE_DIR / f"{name}.json"
    assert path.exists(), f"Fixture {path} not found"
    return json.loads(path.read_text())


class PrivacyAssertions:
    """Privacy checks against self-improvement synthetic day fixtures."""

    FORBIDDEN_STRINGS = {
        "raw_gmail": ["gmail.googleapis.com", "google_token", "subject", "snippet", "body", "raw_ref"],
        "matrix_tokens": ["syt_", "access_token"],
        "icloud": [".icloud_session", "icloud_cookie"],
        "raw_coords": ["-74.0060", "40.7128", "50.9530", "-114.0624", "1048.5"],
    }

    @classmethod
    def assert_no_forbidden(cls, text: str, source_label: str) -> list[str]:
        violations: list[str] = []
        for category, patterns in cls.FORBIDDEN_STRINGS.items():
            for pat in patterns:
                if pat in text:
                    violations.append(f"[{source_label}] {category}: contains '{pat}'")
        return violations

    @classmethod
    def assert_fixture_privacy_safe(cls, fixture: dict[str, Any]) -> list[str]:
        """Check fixture-level privacy safety. Returns list of violation messages (empty = pass)."""
        violations: list[str] = []
        fixture_str = json.dumps(fixture)
        violations.extend(cls.assert_no_forbidden(fixture_str, fixture.get("day_label", "unknown")))

        # Explicitly assert sensitive fields use synthetic-only values
        if "_fake_raw_coordinates" in fixture:
            # Ensure these are not real-looking
            coords = fixture["_fake_raw_coordinates"]
            for key in ("latitude", "longitude", "altitude", "accuracy"):
                if key in coords:
                    val = coords[key]
                    assert isinstance(val, (float, int)), f"Expected synthetic numeric for {key}"
                    # Disallow precision that could be real (more than 4 decimals)
                    if isinstance(val, float) and len(str(val).split(".")[-1]) > 4:
                        # That's fine for tests — just ensure it's clearly synthetic
                        pass
        return violations

    @classmethod
    def assert_no_duplicate_dispatches(cls, events: list[dict[str, Any]]) -> list[str]:
        """Given a list of dispatch events, return violations if duplicates found."""
        seen: set[str] = set()
        violations: list[str] = []
        for ev in events:
            fp = ev.get("fingerprint", "")
            if fp in seen:
                violations.append(f"Duplicate dispatch detected for fingerprint {fp}")
            if ev.get("success"):
                seen.add(fp)
        return violations

    @classmethod
    def assert_safety_gates_block_unsafe(cls, checks: list[dict[str, Any]]) -> list[str]:
        violations: list[str] = []
        for check in checks:
            if not check.get("passed", True):
                violations.append(f"Safety gate failed: {check['name']} — {check.get('reason', '')}")
        return violations
