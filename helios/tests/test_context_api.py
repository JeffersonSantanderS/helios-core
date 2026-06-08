"""Comprehensive test suite for the context_api /api/v1/context endpoint.

Covers:
  - Valid JSON schema and structural correctness (NEW contract)
  - No sensitive keys (tokens, passwords, room_ids, etc.) leak through
  - Graceful degradation when HELIOS_HOME is empty or files are missing
  - Schema stability — all expected top-level keys are always present
  - No coordinate values (lat/lon/alt/coords) in the response
  - /api/v1/health works and returns expected fields
  - Privacy integration: email redaction, token redaction, coordinate stripping
  - Context-API sanitization: host paths redacted, corporate identifiers redacted

DO NOT test build_context_export from stable_exports.py — it's a different module.
DO NOT reference "schema_version" as a top-level key — the contract has api_meta.api_version.
DO NOT reference "engine" as a top-level key — it's inside "runtime".
DO NOT reference "metrics" — the contract doesn't have it at top level.
"""

from __future__ import annotations

import json
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from helios.context_api.app import app
from helios.context_api import __version__ as CONTEXT_API_VERSION
from helios.dashboard.privacy import (
    NEVER_EXPORT_FIELDS,
    PRIVATE_FIELDS,
    sanitize_dict,
)

# ── Expected top-level keys in /api/v1/context contract ─────────────────────

CONTEXT_TOP_LEVEL_KEYS = frozenset({
    "api_meta",
    "calendar",
    "focus",
    "health",
    "location",
    "modules",
    "mood",
    "reminders_count",
    "runtime",
    "weather",
})

# ── Coordinate-related keys that must never appear with raw values ─────────

COORDINATE_KEYS = frozenset({
    "latitude", "longitude", "lat", "lon", "alt", "accuracy",
    "position", "coords", "coordinates", "gps",
})

# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def client() -> TestClient:
    """Provide a FastAPI TestClient for the context_api app."""
    return TestClient(app)


@pytest.fixture
def tmp_helios(tmp_path: Path) -> Path:
    """Create a temporary Helios home directory with realistic context data.

    Writes both context_export.json and latest_status.json with data that
    exercises the full contract builder path.
    """
    context: dict[str, Any] = {
        "engine": "helios",
        "generated_at": "2026-06-07T00:02:49Z",
        "window_days": 7,
        "metrics": {
            "location": {
                "city": "Calgary",
                "region": "Alberta",
                "country": "CA",
                "is_home": True,
                "source": "home_assistant",
                "last_updated": "2026-06-07T12:00:00Z",
                "stale": False,
                "stale_secs": 0,
                "confidence": 1.0,
                # These must be stripped/redacted by the sanitizer
                "latitude": 51.0447,
                "longitude": -114.0719,
                "accuracy": 10.0,
            },
            "weather": {
                "summary": "Partly Cloudy",
                "temperature_label": "18°C",
                "condition": "partly_cloudy",
            },
            "spotify": {
                "track_name": "Lucky",
                "artist": "Radiohead",
                "is_playing": True,
            },
            "reminders": {
                "count": 5,
                "overdue": 1,
            },
        },
        "focus": {
            "state": "active",
            "active_app": {"name": "Cursor"},
        },
        "health": {
            "health_score": 75,
            "label": "good",
            "resting_hr": 65,
        },
        "mood": {
            "2026-06-06": {"score": 7, "label": "good"},
            "2026-06-07": {"score": 8, "label": "great"},
        },
        "calendar": {
            "events": [
                {"title": "Team standup", "start": "2026-06-07T10:00:00Z"},
                {"title": "Lunch with Alex", "start": "2026-06-07T12:30:00Z"},
            ],
        },
    }
    (tmp_path / "context_export.json").write_text(json.dumps(context))

    status: dict[str, Any] = {
        "engine": "helios",
        "version": "6.0.0",
        "generated_at": "2026-06-07T00:02:49Z",
        "health": "healthy",
        "last_tick_at": "2026-06-07T00:02:49Z",
        "modules": {
            "weather": {
                "state": "ok",
                "freshness_secs": 120,
                "confidence": 0.95,
                "consecutive_ok": 10,
                "consecutive_failures": 0,
            },
            "location": {
                "state": "ok",
                "freshness_secs": 60,
                "confidence": 1.0,
                "consecutive_ok": 5,
                "consecutive_failures": 0,
            },
        },
    }
    (tmp_path / "latest_status.json").write_text(json.dumps(status))

    # Ensure data dir exists
    (tmp_path / "data").mkdir(exist_ok=True)

    return tmp_path


@pytest.fixture
def tmp_helios_with_secrets(tmp_path: Path) -> Path:
    """Create a context export containing sensitive fields that must be sanitized."""
    context: dict[str, Any] = {
        "engine": "helios",
        "generated_at": "2026-06-07T00:02:49Z",
        "window_days": 7,
        "metrics": {
            "location": {
                "city": "Calgary",
                "latitude": 51.0447,
                "longitude": -114.0719,
            },
        },
        "focus": {},
        "health": {},
        "mood": {},
        "calendar": {},
        # Sensitive fields that must be stripped
        "token": "super_secret_token_abc123",
        "api_key": "ak-deadbeef",
        "password": "hunter2",
        "room_id": "!abc123:matrix.org",
        "access_token": "MTX_TOKEN",
        "refresh_token": "REF_TOKEN",
        "webhook_url": "https://hooks.example.com/secret",
        "cookie": "session=xyz",
        "session_id": "sess-999",
        "authorization": "Bearer jwt_token_here",
        "homeserver": "https://matrix.example.com",
        "credential": "cred-xxx",
        "oauth": "oauth-token-val",
    }
    (tmp_path / "context_export.json").write_text(json.dumps(context))

    # Status for health checks
    (tmp_path / "latest_status.json").write_text(json.dumps({
        "engine": "helios",
        "health": "healthy",
        "last_tick_at": "2026-06-07T00:02:49Z",
    }))

    (tmp_path / "data").mkdir(exist_ok=True)

    return tmp_path


@pytest.fixture
def empty_helios_home(tmp_path: Path) -> Path:
    """Create a temporary Helios home directory with NO data files.

    Used to verify graceful degradation.
    """
    (tmp_path / "data").mkdir(exist_ok=True)
    return tmp_path


# ── Helper ──────────────────────────────────────────────────────────────────

def _patch_home(home: Path, stack: ExitStack) -> None:
    """Patch HELIOS_HOME in both the context_api app and the data module."""
    from helios.dashboard import data as data_mod
    from helios.context_api import app as app_mod
    stack.enter_context(patch.object(data_mod, "HELIOS_HOME", home))
    stack.enter_context(patch.object(data_mod, "DATA_DIR", home / "data"))
    stack.enter_context(patch.object(app_mod, "HELIOS_HOME", home))


# ── 1. Valid JSON Schema ────────────────────────────────────────────────────

class TestValidJsonSchema:
    """Verify the /api/v1/context endpoint returns valid, well-structured JSON."""

    def test_returns_200(self, client: TestClient, tmp_helios: Path):
        """Context endpoint returns HTTP 200."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        assert resp.status_code == 200

    def test_response_is_valid_json(self, client: TestClient, tmp_helios: Path):
        """Response body parses as valid JSON."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert isinstance(data, dict)

    def test_api_meta_present(self, client: TestClient, tmp_helios: Path):
        """api_meta field is present and has correct sub-keys."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert "api_meta" in data
        meta = data["api_meta"]
        assert "api_version" in meta
        assert "generated_at" in meta
        assert "sanitizer_version" in meta

    def test_api_meta_version_matches_module(self, client: TestClient, tmp_helios: Path):
        """api_meta.api_version matches context_api.__version__."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert data["api_meta"]["api_version"] == CONTEXT_API_VERSION

    def test_runtime_present(self, client: TestClient, tmp_helios: Path):
        """runtime field is present and has expected sub-keys."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert "runtime" in data
        rt = data["runtime"]
        assert "engine" in rt
        assert "version" in rt
        assert "health" in rt
        assert "last_tick_at" in rt

    def test_runtime_engine_is_helios(self, client: TestClient, tmp_helios: Path):
        """runtime.engine identifies helios."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert data["runtime"]["engine"] == "helios"

    def test_location_has_no_coordinates(self, client: TestClient, tmp_helios: Path):
        """location has city/region but no latitude/longitude keys."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        loc = data["location"]
        assert "city" in loc
        assert "latitude" not in loc
        assert "longitude" not in loc
        assert "location_label" in loc

    def test_weather_has_summary(self, client: TestClient, tmp_helios: Path):
        """weather section has summary, temperature, condition."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        weather = data["weather"]
        assert "summary" in weather
        assert "temperature" in weather
        assert "condition" in weather

    def test_calendar_has_count_and_next_event(self, client: TestClient, tmp_helios: Path):
        """calendar has count and next_event_title."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        cal = data["calendar"]
        assert "count" in cal
        assert "next_event_title" in cal

    def test_focus_has_state(self, client: TestClient, tmp_helios: Path):
        """focus section has state and app."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        focus = data["focus"]
        assert "state" in focus
        assert "app" in focus

    def test_health_redacts_values(self, client: TestClient, tmp_helios: Path):
        """health section keeps labels/scores, redacts raw values."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        health = data["health"]
        # 'label' should be kept as-is
        assert health.get("label") == "good"
        # 'health_score' has 'score' in name so it's kept
        assert "health_score" in health
        # 'resting_hr' has no label/score/status/state/category keywords → redacted
        assert health.get("resting_hr") == "[REDACTED]"

    def test_mood_has_latest(self, client: TestClient, tmp_helios: Path):
        """mood section has date, score, label from most recent entry."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        mood = data["mood"]
        assert "date" in mood
        assert "score" in mood
        assert "label" in mood
        # Latest date should be 2026-06-07
        assert mood["date"] == "2026-06-07"
        assert mood["score"] == 8
        assert mood["label"] == "great"

    def test_spotify_present_when_data_exists(self, client: TestClient, tmp_helios: Path):
        """spotify section present when metrics.spotify data exists."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert "spotify" in data
        assert data["spotify"]["track"] == "Lucky"
        assert data["spotify"]["artist"] == "Radiohead"
        assert data["spotify"]["is_playing"] is True

    def test_reminders_count_is_int(self, client: TestClient, tmp_helios: Path):
        """reminders_count is an integer."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert isinstance(data["reminders_count"], int)
        assert data["reminders_count"] == 5

    def test_modules_is_list(self, client: TestClient, tmp_helios: Path):
        """modules is a list of module health dicts."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert isinstance(data["modules"], list)
        assert len(data["modules"]) == 2
        # Each module entry has expected keys
        for mod in data["modules"]:
            assert "name" in mod
            assert "state" in mod
            assert "freshness_secs" in mod
            assert "confidence" in mod

    def test_content_type_is_json(self, client: TestClient, tmp_helios: Path):
        """Response Content-Type is application/json."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        assert "application/json" in resp.headers.get("content-type", "")


# ── 2. No Sensitive Keys ────────────────────────────────────────────────────

class TestNoSensitiveKeys:
    """Verify that never-export and highly-sensitive fields never appear in output."""

    def test_never_export_fields_absent(self, client: TestClient, tmp_helios_with_secrets: Path):
        """Fields in NEVER_EXPORT_FIELDS must be stripped from the response."""
        with ExitStack() as stack:
            _patch_home(tmp_helios_with_secrets, stack)
            resp = client.get("/api/v1/context")

        data = resp.json()
        for field in NEVER_EXPORT_FIELDS:
            assert field not in data, (
                f"NEVER_EXPORT field {field!r} found in top-level response"
            )
            # Check case-insensitive substring match in top-level keys
            for key in data:
                assert field not in key.lower(), (
                    f"NEVER_EXPORT field {field!r} matched by key {key!r}"
                )

    def test_sensitive_values_not_leaked(self, client: TestClient, tmp_helios_with_secrets: Path):
        """Actual secret values must not appear anywhere in the response."""
        secrets = [
            "super_secret_token_abc123",
            "ak-deadbeef",
            "hunter2",
            "MTX_TOKEN",
            "REF_TOKEN",
        ]
        with ExitStack() as stack:
            _patch_home(tmp_helios_with_secrets, stack)
            resp = client.get("/api/v1/context")

        body = resp.text
        for secret in secrets:
            assert secret not in body, (
                f"Secret value {secret!r} leaked in response body"
            )

    def test_matrix_room_id_redacted(self, client: TestClient, tmp_helios_with_secrets: Path):
        """Matrix room ID patterns must be redacted."""
        with ExitStack() as stack:
            _patch_home(tmp_helios_with_secrets, stack)
            resp = client.get("/api/v1/context")

        body = resp.text
        # The raw room ID format !xxx:domain should not appear
        assert "!abc123:matrix.org" not in body

    def test_webhook_url_not_present(self, client: TestClient, tmp_helios_with_secrets: Path):
        """Webhook URLs must be stripped from the response."""
        with ExitStack() as stack:
            _patch_home(tmp_helios_with_secrets, stack)
            resp = client.get("/api/v1/context")

        body = resp.text
        assert "hooks.example.com" not in body
        assert "webhook_url" not in body

    def test_nested_sensitive_fields_stripped(self, client: TestClient, tmp_helios: Path):
        """Sensitive fields nested inside metrics or sub-dicts are removed."""
        context = json.loads((tmp_helios / "context_export.json").read_text())
        context["metrics"]["_internal"] = {
            "token": "nested_secret",
            "password": "nested_pass",
            "room_id": "!nested:example.com",
        }
        (tmp_helios / "context_export.json").write_text(json.dumps(context))

        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")

        data = resp.json()
        # The nested dict should not have sensitive keys at any depth
        body = resp.text
        assert "nested_secret" not in body
        assert "nested_pass" not in body
        assert "!nested:example.com" not in body


# ── 3. Graceful Degradation ─────────────────────────────────────────────────

class TestGracefulDegradation:
    """Verify the context endpoint handles missing/corrupt data gracefully."""

    def test_empty_data_dir_returns_200(self, client: TestClient, empty_helios_home: Path):
        """Endpoint returns 200 even when no data files exist."""
        with ExitStack() as stack:
            _patch_home(empty_helios_home, stack)
            resp = client.get("/api/v1/context")
        assert resp.status_code == 200

    def test_empty_data_dir_returns_valid_json(self, client: TestClient, empty_helios_home: Path):
        """Response is valid JSON even with empty data dir."""
        with ExitStack() as stack:
            _patch_home(empty_helios_home, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert isinstance(data, dict)

    def test_empty_data_dir_has_all_top_level_keys(self, client: TestClient, empty_helios_home: Path):
        """All expected top-level keys present even with empty data dir."""
        with ExitStack() as stack:
            _patch_home(empty_helios_home, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        for key in CONTEXT_TOP_LEVEL_KEYS:
            assert key in data, (
                f"Missing top-level key {key!r} in empty-data response"
            )

    def test_empty_data_dir_no_exception(self, client: TestClient, empty_helios_home: Path):
        """No 'error' field in empty-data response."""
        with ExitStack() as stack:
            _patch_home(empty_helios_home, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        # The old contract had "error" in exception path; the new contract
        # should not have it for simple empty-data cases
        assert "error" not in data or data.get("error") is None

    def test_malformed_context_file_returns_valid_json(self, client: TestClient, tmp_path: Path):
        """Malformed context_export.json is handled gracefully — still valid JSON."""
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "context_export.json").write_text("{invalid json!!!}")
        (tmp_path / "latest_status.json").write_text(json.dumps({
            "engine": "helios",
        }))

        with ExitStack() as stack:
            _patch_home(tmp_path, stack)
            resp = client.get("/api/v1/context")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_missing_context_file_has_api_meta(self, client: TestClient, empty_helios_home: Path):
        """Fallback response when file is missing still has api_meta with api_version."""
        with ExitStack() as stack:
            _patch_home(empty_helios_home, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert "api_meta" in data
        assert "api_version" in data["api_meta"]
        assert data["api_meta"]["api_version"] == CONTEXT_API_VERSION

    def test_missing_context_file_has_runtime(self, client: TestClient, empty_helios_home: Path):
        """Fallback response when files are missing still has runtime.engine."""
        with ExitStack() as stack:
            _patch_home(empty_helios_home, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert "runtime" in data
        assert data["runtime"].get("engine") == "helios"

    def test_empty_data_weather_is_no_data(self, client: TestClient, empty_helios_home: Path):
        """With no data, weather returns {summary: 'no data'}."""
        with ExitStack() as stack:
            _patch_home(empty_helios_home, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert data["weather"] == {"summary": "no data"}

    def test_empty_data_focus_is_unknown(self, client: TestClient, empty_helios_home: Path):
        """With no data, focus returns {state: 'unknown'}."""
        with ExitStack() as stack:
            _patch_home(empty_helios_home, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert data["focus"] == {"state": "unknown"}

    def test_empty_data_mood_is_empty_dict(self, client: TestClient, empty_helios_home: Path):
        """With no data, mood returns empty dict."""
        with ExitStack() as stack:
            _patch_home(empty_helios_home, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert data["mood"] == {}

    def test_empty_data_reminders_count_is_zero(self, client: TestClient, empty_helios_home: Path):
        """With no data, reminders_count is 0."""
        with ExitStack() as stack:
            _patch_home(empty_helios_home, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        assert data["reminders_count"] == 0


# ── 4. Schema Stability ────────────────────────────────────────────────────

class TestSchemaStability:
    """Verify all expected top-level keys are always present regardless of data state."""

    def test_all_top_level_keys_with_data(self, client: TestClient, tmp_helios: Path):
        """All expected keys present when data is fully populated."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        missing = CONTEXT_TOP_LEVEL_KEYS - set(data.keys())
        assert not missing, f"Missing keys: {missing}"

    def test_all_top_level_keys_without_data(self, client: TestClient, empty_helios_home: Path):
        """All expected keys present even when data dir is empty."""
        with ExitStack() as stack:
            _patch_home(empty_helios_home, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        missing = CONTEXT_TOP_LEVEL_KEYS - set(data.keys())
        assert not missing, f"Missing keys with empty data: {missing}"

    def test_no_extra_unexpected_top_level_secrets(self, client: TestClient, tmp_helios: Path):
        """No sensitive/secret top-level keys appear in a healthy response."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        for key in data:
            key_lower = key.lower()
            for field in NEVER_EXPORT_FIELDS:
                assert field not in key_lower, (
                    f"NEVER_EXPORT field {field!r} found in key {key!r}"
                )

    def test_key_types_are_stable(self, client: TestClient, tmp_helios: Path):
        """Top-level key values have stable types."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        # These should always be dicts
        for key in ("api_meta", "runtime", "location", "weather", "calendar",
                     "focus", "health", "mood"):
            assert isinstance(data.get(key), dict), (
                f"Key {key!r} should be dict, got {type(data.get(key))}"
            )
        # modules should always be a list
        assert isinstance(data.get("modules"), list)
        # reminders_count should always be an int
        assert isinstance(data.get("reminders_count"), int)

    def test_runtime_sub_key_types(self, client: TestClient, tmp_helios: Path):
        """runtime sub-keys have stable types."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        rt = data["runtime"]
        assert isinstance(rt.get("engine"), str)
        assert isinstance(rt.get("version"), str)
        assert isinstance(rt.get("health"), str)
        # last_tick_at can be str or None
        assert rt.get("last_tick_at") is None or isinstance(rt.get("last_tick_at"), str)


# ── 5. No Coordinate Values ────────────────────────────────────────────────

class TestNoCoordinateValues:
    """Verify that raw coordinate values never appear in API output."""

    def test_no_latitude_in_location(self, client: TestClient, tmp_helios: Path):
        """location dict has no latitude key (stripped by sanitize_location)."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        loc = data.get("location", {})
        assert "latitude" not in loc
        assert "lat" not in loc

    def test_no_longitude_in_location(self, client: TestClient, tmp_helios: Path):
        """location dict has no longitude key (stripped by sanitize_location)."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        loc = data.get("location", {})
        assert "longitude" not in loc
        assert "lon" not in loc

    def test_no_alt_accuracy_in_location(self, client: TestClient, tmp_helios: Path):
        """location dict has no alt/accuracy keys (stripped by sanitize_location)."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()
        loc = data.get("location", {})
        assert "alt" not in loc
        assert "accuracy" not in loc

    def test_no_raw_coordinate_numbers(self, client: TestClient, tmp_helios: Path):
        """Raw lat/lon numeric values (like 51.0447, -114.0719) never appear in response body."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")

        body = resp.text
        # Coordinate-like floats with 4+ decimal places in the valid range
        coord_pattern = re.compile(r"-?\d{1,3}\.\d{4,}")
        matches = coord_pattern.findall(body)
        for match in matches:
            float_val = float(match)
            if -180 <= float_val <= 180 and abs(float_val) > 10:
                assert False, (
                    f"Potential coordinate value {match} found in response body"
                )

    def test_no_coords_key_in_response(self, client: TestClient, tmp_helios: Path):
        """'coords', 'coordinates', 'position', 'gps' keys are absent or redacted."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")
        data = resp.json()

        def _check_no_raw_coords(d: dict, path: str = "") -> None:
            for key, val in d.items():
                key_lower = key.lower()
                current_path = f"{path}.{key}" if path else key
                if key_lower in COORDINATE_KEYS:
                    assert val == "[REDACTED]" or val is None, (
                        f"Coordinate key {current_path!r} not redacted: {val!r}"
                    )
                if isinstance(val, dict):
                    _check_no_raw_coords(val, current_path)
                if isinstance(val, list):
                    for i, item in enumerate(val):
                        if isinstance(item, dict):
                            _check_no_raw_coords(item, f"{current_path}[{i}]")

        _check_no_raw_coords(data)

    def test_locally_injected_coords_are_sanitized(self):
        """Even if coords are injected post-load, sanitize_dict catches them."""
        raw = {"city": "Test", "lat": 45.0, "lon": -73.0, "position": [45.0, -73.0]}
        result = sanitize_dict(raw)
        assert result.get("lat") == "[REDACTED]"
        assert result.get("lon") == "[REDACTED]"
        # position list that looks like coordinate pair -> redacted
        assert result.get("position") == "[REDACTED]"

    def test_coordinate_list_pair_detected(self):
        """A list of two floats that looks like lat/lon is detected and redacted."""
        raw = {"center": [45.5017, -73.5673]}
        result = sanitize_dict(raw)
        assert result["center"] == "[REDACTED]"

    def test_non_coordinate_list_not_redacted(self):
        """Lists that don't look like coordinates are preserved."""
        raw = {"counts": [3, 7], "items": [1, 2, 3]}
        result = sanitize_dict(raw)
        assert result["counts"] == [3, 7]
        assert result["items"] == [1, 2, 3]


# ── 6. /api/v1/health Endpoint ──────────────────────────────────────────────

class TestV1HealthEndpoint:
    """Verify the /api/v1/health endpoint works correctly."""

    def test_returns_200(self, client: TestClient):
        """V1 health endpoint returns HTTP 200."""
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200

    def test_returns_valid_json(self, client: TestClient):
        """Response is valid JSON."""
        resp = client.get("/api/v1/health")
        data = resp.json()
        assert isinstance(data, dict)

    def test_status_field(self, client: TestClient):
        """Status is 'ok' or 'degraded'."""
        resp = client.get("/api/v1/health")
        data = resp.json()
        assert data["status"] in ("ok", "degraded")

    def test_service_field_is_helios_context_api(self, client: TestClient):
        """Service name is 'helios-context-api'."""
        resp = client.get("/api/v1/health")
        data = resp.json()
        assert data["service"] == "helios-context-api"

    def test_version_field(self, client: TestClient):
        """Version is present and matches context API version."""
        resp = client.get("/api/v1/health")
        data = resp.json()
        assert isinstance(data.get("version"), str)
        assert len(data["version"]) > 0
        assert data["version"] == CONTEXT_API_VERSION

    def test_uptime_secs_field(self, client: TestClient):
        """uptime_secs is a non-negative number."""
        resp = client.get("/api/v1/health")
        data = resp.json()
        assert "uptime_secs" in data
        assert isinstance(data["uptime_secs"], (int, float))
        assert data["uptime_secs"] >= 0

    def test_bind_field(self, client: TestClient):
        """Bind address is 127.0.0.1:8200 (local only)."""
        resp = client.get("/api/v1/health")
        data = resp.json()
        assert data["bind"] == "127.0.0.1:8200"

    def test_missing_sources_field(self, client: TestClient):
        """missing_sources is a list."""
        resp = client.get("/api/v1/health")
        data = resp.json()
        assert isinstance(data.get("missing_sources"), list)

    def test_no_secrets_in_health_response(self, client: TestClient):
        """Health response must not contain tokens, passwords, or secrets."""
        resp = client.get("/api/v1/health")
        body = resp.text.lower()
        for field in ("token", "password", "secret", "api_key", "cookie",
                       "room_id", "access_token", "credential"):
            assert field not in body, (
                f"Sensitive field {field!r} found in /api/v1/health response"
            )

    def test_degraded_when_sources_missing(self, client: TestClient, empty_helios_home: Path):
        """Status is 'degraded' when critical data sources are missing."""
        with ExitStack() as stack:
            _patch_home(empty_helios_home, stack)
            resp = client.get("/api/v1/health")

        data = resp.json()
        assert data["status"] == "degraded"
        assert len(data["missing_sources"]) > 0

    def test_ok_when_sources_present(self, client: TestClient, tmp_helios: Path):
        """Status is 'ok' when critical data sources exist."""
        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/health")

        data = resp.json()
        assert data["status"] == "ok"
        assert data["missing_sources"] == []

    def test_content_type_is_json(self, client: TestClient):
        """Response Content-Type is application/json."""
        resp = client.get("/api/v1/health")
        assert "application/json" in resp.headers.get("content-type", "")


# ── 7. Integration: Context + Privacy Pipeline ──────────────────────────────

class TestContextPrivacyIntegration:
    """End-to-end tests that context data flows through the privacy pipeline."""

    def test_context_endpoint_strips_never_export_fields(self, client: TestClient, tmp_helios: Path):
        """The /api/v1/context endpoint strips NEVER_EXPORT fields."""
        context = json.loads((tmp_helios / "context_export.json").read_text())
        context["token"] = "abc123"
        context["metrics"]["_internal_meta"] = {"api_key": "hidden", "safe_field": "visible"}
        (tmp_helios / "context_export.json").write_text(json.dumps(context))

        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")

        data = resp.json()
        # Never-export fields must be gone
        assert "token" not in data
        # Nested never-export stripped (api_key is in NEVER_EXPORT_FIELDS)
        body = resp.text
        assert "abc123" not in body
        assert "hidden" not in body

    def test_email_in_string_values_redacted(self, client: TestClient, tmp_helios: Path):
        """Email addresses in string values are redacted by _redact_string."""
        context = json.loads((tmp_helios / "context_export.json").read_text())
        context["calendar"]["events"] = [
            {"title": "Lunch with user@example.com", "start": "2026-06-07T12:30:00Z"},
        ]
        (tmp_helios / "context_export.json").write_text(json.dumps(context))

        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")

        body = resp.text
        assert "user@example.com" not in body
        assert "[EMAIL]" in body

    def test_token_pattern_in_string_values_redacted(self, client: TestClient, tmp_helios: Path):
        """Token-like patterns in string values are redacted by sanitize_for_contract."""
        from helios.context_api.sanitize import sanitize_for_contract
        raw = {"summary": "Login with token=abc123def and key=xyz"}
        result = sanitize_for_contract(raw)
        body = json.dumps(result)
        assert "abc123def" not in body
        assert "[TOKEN_REDACTED]" in body

    def test_private_field_word_boundary_matching(self):
        """PRIVATE_FIELDS only match at word boundaries, not substrings."""
        data = {"counts": 42, "raw_coords": [1, 2], "coords": "value"}
        result = sanitize_dict(data)
        assert result["counts"] == 42  # preserved
        assert result["raw_coords"] == "[REDACTED]"  # matches 'coords'
        assert result["coords"] == "[REDACTED]"  # exact match

    def test_deeply_nested_sanitization(self, client: TestClient, tmp_helios: Path):
        """Sanitization works at arbitrary nesting depth via sanitize_for_contract."""
        from helios.context_api.sanitize import sanitize_for_contract
        raw = {
            "deeply": {
                "nested": {
                    "token": "deep_secret",
                    "latitude": 40.7128,
                    "safe": "preserved",
                    "sub": {
                        "password": "deep_pass",
                        "count": 10,
                    },
                },
            },
        }
        result = sanitize_for_contract(raw)
        nested = result.get("deeply", {}).get("nested", {})
        assert "token" not in nested
        assert nested.get("latitude") == "[REDACTED]"
        assert nested.get("safe") == "preserved"
        sub = nested.get("sub", {})
        assert "password" not in sub
        assert sub.get("count") == 10


# ── 8. Context-API Sanitization ─────────────────────────────────────────────

class TestContextApiSanitization:
    """Test context-API-specific sanitization (host paths, corporate IDs)."""

    def test_host_path_redacted(self, client: TestClient, tmp_helios: Path):
        """Host paths like /home/user are redacted in string values by sanitize_for_contract."""
        from helios.context_api.sanitize import sanitize_for_contract
        raw = {"notes": "Config at /home/jefferson/.hermes/helios/config.yaml"}
        result = sanitize_for_contract(raw)
        body = json.dumps(result)
        assert "/home/jefferson" not in body
        assert "[HOME]" in body
        assert ".hermes/helios" not in body
        assert "[HERMES_PATH]" in body

    def test_corporate_identifier_redacted(self, client: TestClient, tmp_helios: Path):
        """Corporate identifiers like 'santander' are redacted in string values."""
        context = json.loads((tmp_helios / "context_export.json").read_text())
        context["corporate_note"] = "Access the santander_network VPN"
        (tmp_helios / "context_export.json").write_text(json.dumps(context))

        with ExitStack() as stack:
            _patch_home(tmp_helios, stack)
            resp = client.get("/api/v1/context")

        body = resp.text
        # 'santander' should be redacted
        assert "santander" not in body.lower()
        # The redacted version replaces with [REDACTED]
        assert "[REDACTED]" in body

    def test_no_host_paths_in_health_response(self, client: TestClient):
        """Health response must not contain any host-specific paths."""
        resp = client.get("/api/v1/health")
        body = resp.text
        for pattern in ("/home/", "/Users/", "~/.hermes", "/mnt/c/Users"):
            assert pattern not in body, (
                f"Host path pattern {pattern!r} found in health response"
            )

    def test_wsl_path_redacted(self, client: TestClient, tmp_helios: Path):
        """WSL paths like /mnt/c/Users/... are redacted by sanitize_for_contract."""
        from helios.context_api.sanitize import sanitize_for_contract
        raw = {"path_info": "File at /mnt/c/Users/jefferson/Desktop"}
        result = sanitize_for_contract(raw)
        body = json.dumps(result)
        assert "/mnt/c/Users/jefferson" not in body
        assert "[HOME]" in body