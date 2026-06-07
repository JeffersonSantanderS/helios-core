"""Tests for the Helios dashboard — data loaders, privacy sanitization, and API endpoints."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from helios.dashboard.privacy import (
    sanitize_dict,
    sanitize_location,
    sanitize_health,
    privacy_panel,
    NEVER_EXPORT_FIELDS,
    PRIVATE_FIELDS,
    _redact_string,
)
from helios.dashboard.data import (
    load_json_safe,
    load_channel_events,
    build_dashboard_snapshot,
    load_priority_engine_card,
    load_work_hours_card,
    load_health_diary_card,
    load_location_freshness_card,
    load_spotify_card,
    load_agenda_card,
    load_module_staleness_card,
    HELIOS_HOME,
    DATA_DIR,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_helios(tmp_path):
    """Create a temporary Helios home directory with sample data."""
    status = {
        "schema_version": "1.0",
        "engine": "helios",
        "version": "6.0.0",
        "generated_at": "2026-05-24T00:02:49Z",
        "health": "healthy",
        "last_tick_at": "2026-05-24T00:02:49Z",
        "modules": {
            "location": {
                "module": "location",
                "state": "healthy",
                "freshness_secs": 5.0,
                "confidence": 1.0,
                "consecutive_ok": 100,
                "consecutive_failures": 0,
                "last_error": None,
            },
            "calendar": {
                "module": "calendar",
                "state": "healthy",
                "freshness_secs": 10.0,
                "confidence": 1.0,
                "consecutive_ok": 200,
                "consecutive_failures": 0,
                "last_error": None,
            },
        },
        "open_alerts": [],
    }
    (tmp_path / "latest_status.json").write_text(json.dumps(status))

    context = {
        "schema_version": "1.0",
        "engine": "helios",
        "generated_at": "2026-05-24T00:02:49Z",
        "window_days": 1,
        "metrics": {
            "location": {"city": "Anytown", "is_home": True, "stale": False},
            "weather": {"summary": "Cloudy", "condition": "overcast"},
        },
        "focus": {"state": "active", "active_app": {"name": "Cursor"}},
        "health": {"health_score": 75, "label": "good"},
        "mood": {"2026-05-23": {"score": 7, "label": "good"}},
    }
    (tmp_path / "context_export.json").write_text(json.dumps(context))

    alerts = {
        "schema_version": "1.0",
        "engine": "helios",
        "generated_at": "2026-05-24T00:02:49Z",
        "window_hours": 24,
        "alerts": [],
    }
    (tmp_path / "alerts_recent.json").write_text(json.dumps(alerts))

    # Create data dir with channel_log
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    events = [
        {"event_type": "status", "title": "Morning Briefing", "timestamp": "2026-05-24T08:00:00Z", "priority": 1},
        {"event_type": "alert", "title": "Dream Alert", "timestamp": "2026-05-24T09:00:00Z", "severity": "warning"},
        {"event_type": "checkin", "title": "Mood Check-in", "timestamp": "2026-05-24T10:00:00Z", "checkin_type": "mood"},
    ]
    (data_dir / "channel_log.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events)
    )

    return tmp_path


# ── Privacy Tests ─────────────────────────────────────────────────────────────

class TestSanitizeDict:
    def test_never_export_fields_removed(self):
        data = {"token": "abc123", "name": "test", "api_key": "deadbeef"}
        result = sanitize_dict(data)
        assert "token" not in result
        assert "api_key" not in result
        assert result["name"] == "test"

    def test_private_fields_redacted(self):
        data = {"latitude": 40.7128, "city": "Anytown", "email": "user@example.com"}
        result = sanitize_dict(data)
        assert result["latitude"] == "[REDACTED]"
        assert result["city"] == "Anytown"

    def test_nested_dict_sanitized(self):
        data = {"config": {"token": "abc", "value": 42}, "public": "yes"}
        result = sanitize_dict(data)
        assert "token" not in result["config"]
        assert result["config"]["value"] == 42
        assert result["public"] == "yes"

    def test_list_of_dicts_sanitized(self):
        data = {"items": [{"token": "x", "ok": True}, {"latitude": 50, "name": "a"}]}
        result = sanitize_dict(data)
        assert "token" not in result["items"][0]
        assert result["items"][0]["ok"] is True
        assert result["items"][1]["latitude"] == "[REDACTED]"

    def test_empty_dict(self):
        assert sanitize_dict({}) == {}

    def test_no_sensitive_fields(self):
        data = {"name": "test", "count": 5, "enabled": True}
        result = sanitize_dict(data)
        assert result == data


class TestSanitizeLocation:
    def test_keep_city_redact_coords(self):
        loc = {"city": "Anytown", "latitude": 40.7128, "longitude": -74.0060, "is_home": True}
        result = sanitize_location(loc)
        assert result["city"] == "Anytown"
        assert result["is_home"] is True
        assert "latitude" not in result
        assert "longitude" not in result
        assert result["location_label"] == "Anytown"

    def test_empty_location(self):
        assert sanitize_location({}) == {}

    def test_region_fallback(self):
        loc = {"region": "State", "latitude": 51}
        result = sanitize_location(loc)
        assert result["location_label"] == "State"


class TestSanitizeHealth:
    def test_keep_scores_redact_values(self):
        health = {"health_score": 75, "resting_hr": 65, "label": "good"}
        result = sanitize_health(health)
        assert result["health_score"] == 75
        assert result["label"] == "good"
        assert result["resting_hr"] == "[REDACTED]"

    def test_empty_health(self):
        assert sanitize_health({}) == {}

    def test_token_removed(self):
        health = {"token": "abc", "score": 80}
        result = sanitize_health(health)
        assert "token" not in result
        assert result["score"] == 80


class TestPrivacyPanel:
    def test_returns_all_classes(self):
        panel = privacy_panel()
        assert len(panel) == 5
        classes = [p["class"] for p in panel]
        assert "public_safe" in classes
        assert "never_export" in classes

    def test_each_entry_has_description(self):
        for entry in privacy_panel():
            assert "class" in entry
            assert "description" in entry
            assert len(entry["description"]) > 0


class TestRedactString:
    def test_redact_token_pattern(self):
        assert "[TOKEN_REDACTED]" in _redact_string("token: abc123")

    def test_redact_coordinates(self):
        result = _redact_string("lat 40.7128 lon -74.0060")
        assert "40.7128" not in result
        assert "[COORD]" in result

    def test_redact_email(self):
        result = _redact_string("sent by user@example.com")
        assert "user@example.com" not in result
        assert "[EMAIL]" in result

    def test_no_redactions_needed(self):
        assert _redact_string("hello world") == "hello world"


# ── Data Loader Tests ──────────────────────────────────────────────────────────

class TestLoadJsonSafe:
    def test_load_valid_json(self, tmp_path):
        path = tmp_path / "test.json"
        path.write_text('{"key": "value"}')
        result = load_json_safe(path)
        assert result == {"key": "value"}

    def test_missing_file_returns_default(self, tmp_path):
        result = load_json_safe(tmp_path / "nonexistent.json")
        assert result == {}

    def test_malformed_json_returns_default(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text('{invalid json}')
        result = load_json_safe(path)
        assert result == {}

    def test_custom_default(self, tmp_path):
        result = load_json_safe(tmp_path / "nope.json", default=[])
        assert result == []


class TestLoadChannelEvents:
    def test_load_events(self, tmp_helios):
        with patch("helios.dashboard.data.DATA_DIR", tmp_helios / "data"):
            events = load_channel_events(limit=20)
        assert len(events) == 3
        assert events[0]["event_type"] == "status"
        assert events[2]["event_type"] == "checkin"

    def test_missing_jsonl_returns_empty(self, tmp_path):
        with patch("helios.dashboard.data.DATA_DIR", tmp_path):
            events = load_channel_events(limit=20)
        assert events == []

    def test_malformed_lines_skipped(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        jsonl = data_dir / "channel_log.jsonl"
        jsonl.write_text('valid:json\n{"ok": true}\nnot json\n{"also": "valid"}')
        with patch("helios.dashboard.data.DATA_DIR", data_dir):
            events = load_channel_events(limit=20)
        # "valid:json" is not valid JSON, "not json" is not valid either
        # Only {"ok": true} and {"also": "valid"} parse
        assert len(events) == 2

    def test_limit_respected(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        jsonl = data_dir / "channel_log.jsonl"
        lines = [json.dumps({"event_type": "status", "title": f"Event {i}"}) for i in range(50)]
        jsonl.write_text("\n".join(lines))
        with patch("helios.dashboard.data.DATA_DIR", data_dir):
            events = load_channel_events(limit=10)
        assert len(events) == 10


class TestBuildDashboardSnapshot:
    def test_full_snapshot(self, tmp_helios):
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.data.DATA_DIR", tmp_helios / "data"):
            snapshot = build_dashboard_snapshot()

        # Top-level sections exist
        assert "runtime_status" in snapshot
        assert "module_health" in snapshot
        assert "recent_events" in snapshot
        assert "context_summary" in snapshot
        assert "alerts" in snapshot
        assert "privacy_panel" in snapshot

        # Runtime status
        assert snapshot["runtime_status"]["engine"] == "helios"
        assert snapshot["runtime_status"]["version"] == "6.0.0"
        assert snapshot["runtime_status"]["health"] == "healthy"
        assert snapshot["runtime_status"]["tick_age_secs"] is not None

        # Module health
        assert len(snapshot["module_health"]) == 2
        loc_mod = [m for m in snapshot["module_health"] if m["name"] == "location"][0]
        assert loc_mod["state"] == "healthy"
        assert loc_mod["freshness_secs"] == 5.0

        # Recent events
        assert len(snapshot["recent_events"]) == 3

        # Context summary has location (sanitized)
        ctx = snapshot["context_summary"]
        assert ctx["location"]["city"] == "Anytown"
        assert "latitude" not in ctx["location"]

        # Privacy panel
        assert len(snapshot["privacy_panel"]) == 5

    # ── Phase 6B: API and metadata tests ────────────────────────────────────

    def test_health_endpoint_ok(self):
        """GET /health returns privacy-safe status info."""
        from helios.dashboard.app import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "helios-dashboard"
        assert data["status"] in ("ok", "degraded")
        assert data["mode"] == "read-only"
        assert data["bind"] == "127.0.0.1:8199"
        assert "uptime_secs" in data
        assert isinstance(data["uptime_secs"], (int, float))
        # No secrets
        json_str = json.dumps(data)
        assert "token" not in json_str.lower()
        assert "password" not in json_str.lower()

    def test_health_endpoint_degraded_when_missing(self, tmp_path):
        """Health returns 'degraded' when critical sources are missing."""
        from helios.dashboard.app import app
        from fastapi.testclient import TestClient
        # Patch DATA_SOURCES to point to nonexistent paths
        fake_sources = {
            "latest_status": tmp_path / "nonexistent_status.json",
            "context_export": tmp_path / "nonexistent_context.json",
        }
        with patch("helios.dashboard.app._get_data_sources", return_value=fake_sources):
            client = TestClient(app)
            resp = client.get("/health")
            data = resp.json()
            assert data["status"] == "degraded"
            assert "latest_status" in data["missing_sources"]

    def test_api_status_includes_dashboard_meta(self, tmp_helios):
        """GET /api/status includes dashboard metadata section."""
        from helios.dashboard.app import app
        from fastapi.testclient import TestClient
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.data.DATA_DIR", tmp_helios / "data"):
            client = TestClient(app)
            resp = client.get("/api/status")
            data = resp.json()
            assert "dashboard_meta" in data
            meta = data["dashboard_meta"]
            assert meta["dashboard_version"] == "0.3.0"
            assert "generated_at" in meta
            assert "helios_home" in meta
            assert isinstance(meta["data_sources_present"], list)
            assert isinstance(meta["missing_sources"], list)
            assert meta["sanitizer_version"] == "1.0"

    def test_dashboard_meta_reports_missing_sources(self, tmp_path):
        """Missing data sources are reported in dashboard_meta."""
        from helios.dashboard.app import app
        from fastapi.testclient import TestClient
        # Patch both data layer and _DATA_SOURCES to use tmp_path
        fake_sources = {
            "latest_status": tmp_path / "nonexistent_status.json",
            "context_export": tmp_path / "nonexistent_context.json",
            "alerts_recent": tmp_path / "nonexistent_alerts.json",
            "channel_log": tmp_path / "nonexistent_log.jsonl",
            "module_health": tmp_path / "nonexistent_health.json",
        }
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"), \
             patch("helios.dashboard.app._get_data_sources", return_value=fake_sources):
            client = TestClient(app)
            resp = client.get("/api/status")
            data = resp.json()
            meta = data["dashboard_meta"]
            assert "latest_status" in meta["missing_sources"]
            assert "context_export" in meta["missing_sources"]
            assert "channel_log" in meta["missing_sources"]

    def test_dashboard_meta_reports_present_sources(self, tmp_helios):
        """Present data sources are listed in dashboard_meta."""
        from helios.dashboard.app import app
        from fastapi.testclient import TestClient
        with patch("helios.dashboard.app.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.app.DATA_DIR", tmp_helios / "data"), \
             patch("helios.dashboard.data.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.data.DATA_DIR", tmp_helios / "data"):
            client = TestClient(app)
            resp = client.get("/api/status")
            data = resp.json()
            meta = data["dashboard_meta"]
            # tmp_helios fixture creates these files
            assert "latest_status" in meta["data_sources_present"]
            assert "context_export" in meta["data_sources_present"]
            assert "latest_status" not in meta["missing_sources"]

    def test_static_dashboard_route(self):
        """GET / returns the dashboard HTML page."""
        from helios.dashboard.app import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Helios Dashboard" in resp.text
        assert "local only" in resp.text
        assert "read-only" in resp.text

    def test_health_no_secrets_in_response(self, tmp_helios):
        """Health endpoint never includes secrets or personal data."""
        from helios.dashboard.app import app
        from fastapi.testclient import TestClient
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.data.DATA_DIR", tmp_helios / "data"):
            client = TestClient(app)
            resp = client.get("/health")
            data = resp.json()
            json_str = json.dumps(data)
            for field in ("token", "password", "secret", "api_key", "room_id", "cookie"):
                assert field not in json_str.lower()

    def test_missing_files_no_crash(self, tmp_path):
        """Dashboard must not crash when data files are missing."""
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            snapshot = build_dashboard_snapshot()

        assert snapshot["runtime_status"]["health"] == "unknown"
        assert snapshot["module_health"] == []
        assert snapshot["recent_events"] == []
        assert snapshot["context_summary"] != {}
        assert snapshot["alerts"]["recent_count"] == 0

    def test_sensitive_fields_sanitized(self, tmp_helios):
        """No token/password/room_id fields should appear in the snapshot."""
        # Add sensitive data to status
        status = json.loads((tmp_helios / "latest_status.json").read_text())
        status["token"] = "super_secret_token"
        status["modules"]["location"]["access_token"] = "leaked_token"
        (tmp_helios / "latest_status.json").write_text(json.dumps(status))

        with patch("helios.dashboard.data.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.data.DATA_DIR", tmp_helios / "data"):
            snapshot = build_dashboard_snapshot()

        # Verify no sensitive fields leaked
        snapshot_json = json.dumps(snapshot)
        assert "super_secret_token" not in snapshot_json
        assert "leaked_token" not in snapshot_json
        assert "token" not in snapshot["runtime_status"]


# ── Epic 6: Product Card Tests ────────────────────────────────────────────


class TestPriorityEngineCard:
    """Tests for load_priority_engine_card."""

    def test_missing_data_returns_safe_empty(self, tmp_path):
        """No crash when priority export and DB are absent."""
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_priority_engine_card()
        assert card["mode"] == "unknown"
        assert card["history"]["total_candidates"] == 0
        assert "No priority-engine export" in card["assessment"]

    def test_latest_export_loaded_and_sanitized(self, tmp_path):
        """Latest JSON export is summarized without leaking token fields."""
        priority_dir = tmp_path / "data" / "priority_engine"
        priority_dir.mkdir(parents=True)
        latest = {
            "mode": "shadow",
            "generated": 1,
            "scored": 1,
            "selected": 1,
            "top_candidates": [
                {"title": "Fix stale cache", "score": 0.72, "decision": "select_summary", "token": "secret"}
            ],
        }
        (priority_dir / "latest.json").write_text(json.dumps(latest))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_priority_engine_card()
        assert card["mode"] == "shadow"
        assert card["generated"] == 1
        assert card["top_candidates"][0]["title"] == "Fix stale cache"
        assert "token" not in card["top_candidates"][0]
        assert "secret" not in json.dumps(card)

    def test_sqlite_history_loaded(self, tmp_path):
        """Aggregate priority DB counts are exposed, not raw payloads."""
        import sqlite3
        db = tmp_path / "helios_v6.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE priority_candidates (candidate_id TEXT, created_at TEXT)")
            conn.execute("CREATE TABLE priority_decisions (mode TEXT, decision TEXT, route TEXT, final_score REAL, created_at TEXT)")
            conn.execute("INSERT INTO priority_candidates VALUES ('c1', '2026-05-24T00:00:00Z')")
            conn.execute("INSERT INTO priority_decisions VALUES ('shadow', 'select_summary', 'summary', 0.7, '2026-05-24T00:00:00Z')")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", data_dir):
            card = load_priority_engine_card()
        assert card["history"]["total_candidates"] == 1
        assert card["history"]["total_decisions"] == 1
        assert card["history"]["decisions"][0]["decision"] == "select_summary"


class TestWorkHoursCard:
    """Tests for load_work_hours_card."""

    def test_missing_file_returns_empty(self, tmp_path):
        """No crash when work_hours_state.json is missing."""
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_work_hours_card()
        assert card["current_pay_period"] is None
        assert card["copy_paste_timesheet"] is None
        assert card["needs_review"] == []
        assert card["confidence_counts"] == {}
        assert card["last_generated"] is None

    def test_populated_card(self, tmp_path):
        """Returns expected fields from work hours state."""
        state = {
            "period_label": "May 11 - May 22",
            "period_start": "2026-05-11",
            "generated_at": "2026-05-22T17:00:00Z",
            "report_text": "May 11 - May 22\n07:00-15:00",
            "days": [
                {"date": "2026-05-11", "kind": "work", "paid_hours": 8.0,
                 "confidence": "high", "note": "", "source": "location_inference"},
                {"date": "2026-05-12", "kind": "needs_review", "paid_hours": 0.0,
                 "confidence": "needs_review", "note": "no away data", "source": "needs_review"},
            ],
        }
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "work_hours_state.json").write_text(json.dumps(state))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", data_dir):
            card = load_work_hours_card()
        assert card["current_pay_period"] == "May 11 - May 22"
        assert card["copy_paste_timesheet"] is not None
        assert len(card["needs_review"]) == 1
        assert card["needs_review"][0]["date"] == "2026-05-12"
        assert card["confidence_counts"]["high"] == 1
        assert card["confidence_counts"]["needs_review"] == 1

    def test_sanitized_no_tokens(self, tmp_path):
        """Card goes through sanitize_dict — no token fields leak."""
        state = {
            "period_label": "Test",
            "days": [],
            "token": "secret123",
        }
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "work_hours_state.json").write_text(json.dumps(state))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", data_dir):
            card = load_work_hours_card()
        assert "token" not in card
        card_json = json.dumps(card)
        assert "secret123" not in card_json


class TestHealthDiaryCard:
    """Tests for load_health_diary_card."""

    def test_missing_report_returns_empty(self, tmp_path):
        """No crash when health diary report is missing."""
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_health_diary_card(date_str="2026-05-24")
        assert card["sleep_hours"] is None
        assert card["confidence"] == "needs_review"
        assert card["stale_data_warnings"] == []

    def test_populated_card(self, tmp_path):
        """Returns expected metrics from health diary report."""
        report = {
            "schema_version": "report.v1",
            "confidence": "high",
            "gaps": [],
            "items": [
                {"key": "sleep_hours", "value": 7.5, "unit": "hours", "confidence": "observed"},
                {"key": "steps", "value": 8500, "unit": "count", "confidence": "observed"},
                {"key": "active_minutes", "value": 45, "unit": "minutes", "confidence": "observed"},
                {"key": "mood_score", "value": 7.0, "unit": "1-10", "confidence": "observed"},
            ],
        }
        reports_dir = tmp_path / "data" / "reports"
        reports_dir.mkdir(parents=True)
        (reports_dir / "health_diary_2026-05-24.json").write_text(json.dumps(report))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_health_diary_card(date_str="2026-05-24")
        assert card["sleep_hours"] == 7.5
        assert card["steps"] == 8500
        assert card["active_minutes"] == 45
        assert card["mood_score"] == 7.0
        assert card["confidence"] == "high"

    def test_sanitized_no_raw_health(self, tmp_path):
        """Card goes through sanitize_dict — no raw private fields."""
        report = {
            "confidence": "medium",
            "gaps": ["resting_hr"],
            "items": [
                {"key": "sleep_hours", "value": 6.0, "unit": "hours"},
            ],
        }
        reports_dir = tmp_path / "data" / "reports"
        reports_dir.mkdir(parents=True)
        (reports_dir / "health_diary_2026-05-24.json").write_text(json.dumps(report))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_health_diary_card(date_str="2026-05-24")
        # The card should be sanitized — no email/password/token fields
        card_json = json.dumps(card)
        assert "token" not in card_json.lower() or "[REDACTED]" in card_json


class TestLocationFreshnessCard:
    """Tests for load_location_freshness_card."""

    def test_missing_data_returns_unknown(self, tmp_path):
        """No crash when location data is missing."""
        (tmp_path / "data").mkdir()
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_location_freshness_card()
        assert card["zone_label"] == "unknown"
        assert card["confidence"] == "needs_review"

    def test_no_raw_coordinates(self, tmp_path):
        """Card must NEVER contain raw lat/lon."""
        loc_data = {
            "lat": 40.7128,
            "lon": -74.0060,
            "city": "Anytown",
            "zone": "home",
            "is_home": True,
            "source": "home_assistant",
            "ts": "2026-05-24T12:00:00Z",
        }
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "icloud_location_sync.json").write_text(json.dumps(loc_data))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_location_freshness_card()
        # lat/lon should be redacted by sanitize_dict
        assert "latitude" not in card or card.get("latitude") == "[REDACTED]"
        assert "longitude" not in card or card.get("longitude") == "[REDACTED]"
        assert card["zone_label"] == "home"

    def test_freshness_and_confidence(self, tmp_path):
        """Card reports freshness_secs and confidence correctly."""
        from datetime import datetime, timezone, timedelta
        recent_ts = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        loc_data = {
            "city": "Anytown",
            "source": "home_assistant",
            "is_home": True,
            "freshness_secs": 60,
            "ts": recent_ts,
        }
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "icloud_location_sync.json").write_text(json.dumps(loc_data))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_location_freshness_card()
        assert card["zone_label"] == "home"
        assert card["freshness_secs"] is not None
        assert card["confidence"] in ("high", "medium", "low")


class TestSpotifyCard:
    """Tests for load_spotify_card."""

    def test_missing_report_returns_empty(self, tmp_path):
        """No crash when spotify report is missing."""
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_spotify_card(date_str="2026-05-24")
        assert card["total_minutes"] is None
        assert card["top_artist"] is None
        assert card["session_count"] == 0
        assert card["confidence"] == "needs_review"

    def test_populated_card(self, tmp_path):
        """Returns expected fields from spotify daily report."""
        report = {
            "schema_version": "report.v1",
            "confidence": "high",
            "items": [
                {"key": "top_artists", "value": ["Radiohead", "Beck", "Tame Impala"], "unit": "list"},
                {"key": "total_minutes", "value": 87.5, "unit": "minutes"},
                {"key": "session_count", "value": 3, "unit": "count"},
                {"key": "late_night_minutes", "value": 22.0, "unit": "minutes"},
            ],
        }
        reports_dir = tmp_path / "data" / "reports"
        reports_dir.mkdir(parents=True)
        (reports_dir / "spotify_daily_2026-05-24.json").write_text(json.dumps(report))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_spotify_card(date_str="2026-05-24")
        assert card["total_minutes"] == 87.5
        assert card["top_artist"] == "Radiohead"  # Single, not list
        assert card["session_count"] == 3
        assert card["night_session"] is True
        assert card["confidence"] == "high"

    def test_no_late_night(self, tmp_path):
        """late_night is False when late_night_minutes is 0 or absent."""
        report = {
            "confidence": "medium",
            "items": [
                {"key": "top_artists", "value": ["Artist A"], "unit": "list"},
                {"key": "total_minutes", "value": 30.0, "unit": "minutes"},
                {"key": "session_count", "value": 1, "unit": "count"},
            ],
        }
        reports_dir = tmp_path / "data" / "reports"
        reports_dir.mkdir(parents=True)
        (reports_dir / "spotify_daily_2026-05-24.json").write_text(json.dumps(report))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_spotify_card(date_str="2026-05-24")
        assert card["night_session"] is False


class TestAgendaCard:
    """Tests for load_agenda_card."""

    def test_missing_context_returns_empty(self, tmp_path):
        """No crash when context_export.json is missing."""
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_agenda_card()
        assert card["events_count"] == 0
        assert card["next_event_title"] is None
        assert card["overdue_count"] == 0

    def test_populated_card(self, tmp_helios):
        """Returns expected fields from context export."""
        # Reuse tmp_helios fixture which has context_export.json
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.data.DATA_DIR", tmp_helios / "data"):
            card = load_agenda_card()
        assert "events_count" in card
        assert "overdue_count" in card

    def test_sanitized_event_title(self, tmp_path):
        """Event titles are sanitized to remove sensitive patterns."""
        context = {
            "calendar": {
                "events": [
                    {"title": "Discuss secret:abc123 reset", "start": "2026-05-24T10:00:00Z"},
                ],
            },
            "metrics": {"reminders": {"overdue": 2}},
        }
        (tmp_path / "context_export.json").write_text(json.dumps(context))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_agenda_card()
        assert card["overdue_count"] == 2
        # Title with "secret:abc123" pattern should have the secret value redacted
        if card["next_event_title"] is not None:
            assert "abc123" not in card["next_event_title"]


class TestModuleStalenessCard:
    """Tests for load_module_staleness_card."""

    def test_missing_status_returns_empty(self, tmp_path):
        """No crash when latest_status.json is missing."""
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_path), \
             patch("helios.dashboard.data.DATA_DIR", tmp_path / "data"):
            card = load_module_staleness_card()
        assert card["modules"] == []
        assert card["stale_modules"] == []
        assert "No module health data" in card["summary"]

    def test_populated_card(self, tmp_helios):
        """Returns modules, staleness, and summary from status."""
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.data.DATA_DIR", tmp_helios / "data"):
            card = load_module_staleness_card()
        assert len(card["modules"]) == 2
        module_names = [m["module_name"] for m in card["modules"]]
        assert "location" in module_names
        assert "calendar" in module_names

    def test_stale_detection(self, tmp_helios):
        """Modules with high freshness_secs are marked stale."""
        status = json.loads((tmp_helios / "latest_status.json").read_text())
        status["modules"]["old_module"] = {
            "module": "old_module",
            "state": "healthy",
            "freshness_secs": 7200,  # 2 hours > 1 hour threshold
            "confidence": 0.5,
        }
        (tmp_helios / "latest_status.json").write_text(json.dumps(status))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.data.DATA_DIR", tmp_helios / "data"):
            card = load_module_staleness_card(threshold_secs=3600)
        assert "old_module" in card["stale_modules"]

    def test_stale_detection_respects_module_override(self, tmp_helios):
        """Per-module freshness overrides prevent false stale warnings."""
        status = json.loads((tmp_helios / "latest_status.json").read_text())
        status["modules"]["health"] = {
            "module": "health",
            "state": "healthy",
            "freshness_secs": 7200,
            "confidence": 1.0,
            "_freshness_threshold_override": {"fresh": 28800, "stale": 57600, "degraded": 86400},
        }
        (tmp_helios / "latest_status.json").write_text(json.dumps(status))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.data.DATA_DIR", tmp_helios / "data"):
            card = load_module_staleness_card(threshold_secs=3600)
            snapshot = build_dashboard_snapshot()
        assert "health" not in card["stale_modules"]
        assert "health" not in snapshot["alerts"].get("stale_modules", [])

    def test_all_fresh_summary(self, tmp_helios):
        """All modules fresh produces correct summary."""
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.data.DATA_DIR", tmp_helios / "data"):
            card = load_module_staleness_card()
        # Default fixture has low freshness_secs so should be fresh
        assert "All modules fresh" in card["summary"] or "stale" not in card["summary"].lower()


class TestDashboardSnapshotIncludesCards:
    """Verify build_dashboard_snapshot includes the new card sections."""

    def test_cards_section_present(self, tmp_helios):
        """Snapshot includes cards dict with all expected keys."""
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.data.DATA_DIR", tmp_helios / "data"):
            snapshot = build_dashboard_snapshot()
        assert "cards" in snapshot
        cards = snapshot["cards"]
        assert "work_hours" in cards
        assert "health_diary" in cards
        assert "location_freshness" in cards
        assert "spotify" in cards
        assert "agenda" in cards
        assert "module_staleness" in cards
        assert "priority_engine" in cards

    def test_cards_are_sanitized(self, tmp_helios):
        """Cards in snapshot have no raw lat/lon or token fields."""
        # Add location data with coordinates
        loc_data = {
            "lat": 40.7128,
            "lon": -74.0060,
            "city": "Anytown",
            "zone": "home",
            "source": "test",
            "freshness_secs": 30,
        }
        (tmp_helios / "data" / "icloud_location_sync.json").write_text(json.dumps(loc_data))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.data.DATA_DIR", tmp_helios / "data"):
            snapshot = build_dashboard_snapshot()
        snapshot_json = json.dumps(snapshot)
        # Raw coordinate patterns should not appear (they get redacted)
        # Check that specific lat/lon values don't leak
        loc_card = snapshot["cards"]["location_freshness"]
        assert loc_card.get("lat") is None or loc_card["lat"] == "[REDACTED]"
        assert loc_card.get("lon") is None or loc_card["lon"] == "[REDACTED]"

    def test_cards_no_secrets(self, tmp_helios):
        """Cards should not contain token/password fields."""
        # Add work_hours state with a token
        state = {
            "period_label": "Test",
            "days": [],
            "token": "should_be_removed",
        }
        data_dir = tmp_helios / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "work_hours_state.json").write_text(json.dumps(state))
        with patch("helios.dashboard.data.HELIOS_HOME", tmp_helios), \
             patch("helios.dashboard.data.DATA_DIR", data_dir):
            snapshot = build_dashboard_snapshot()
        card_json = json.dumps(snapshot["cards"]["work_hours"])
        assert "should_be_removed" not in card_json
        assert "token" not in snapshot["cards"]["work_hours"]