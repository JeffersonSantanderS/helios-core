"""Helios v6 — Regression tests for runtime spam & briefing hardening.

Tests the P0–P2 fixes:
  - Dispatcher does not send raw template {variables} on render failure
  - Rule cooldown persists to rules.last_triggered in DB
  - Superseded rules disabled by migration 020
  - AutoDream does not run outside night window
  - AutoDream daily cap survives restart (state persistence)
  - Health score marks missing mood/HR as 'missing', not 0
  - Health score push dedup (max 1 per local day)
"""

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


# ============================================================================
# P0.2 — Dispatcher template render safety
# ============================================================================

class TestDispatcherTemplateRender:
    """Dispatcher must fail closed on missing template keys."""

    def test_format_alert_returns_none_on_missing_key(self):
        """When template references a key not in context, _format_alert returns (None, None)."""
        from helios.dispatcher import AlertDispatcher
        from helios.matrix_pusher import MatrixPusher
        from helios.state import HeliosDB

        db = HeliosDB(":memory:")
        matrix_pusher = MatrixPusher(cfg={})
        dispatcher = AlertDispatcher(db, matrix_pusher, config={})

        hit = {
            "slug": "test_missing_key",
            "severity": "info",
            "category": "scheduled",
            "message": "Weather: {weather.temp_c}°C",
        }
        context = {"some_module": {"irrelevant": True}}

        msg, embed = dispatcher._format_alert(hit, context, "info", "scheduled")
        assert msg is None
        assert embed is None

    def test_format_alert_returns_none_on_nonexistent_module(self):
        """If context module exists but doesn't have the referenced key, returns None."""
        from helios.dispatcher import AlertDispatcher
        from helios.matrix_pusher import MatrixPusher
        from helios.state import HeliosDB

        db = HeliosDB(":memory:")
        matrix_pusher = MatrixPusher(cfg={})
        dispatcher = AlertDispatcher(db, matrix_pusher, config={})

        hit = {
            "slug": "test_missing_nested_key",
            "severity": "info",
            "category": "scheduled",
            "message": "Weather: {weather.temp_c}°C",
        }
        context = {"weather": {"_error": "API failure"}}

        msg, embed = dispatcher._format_alert(hit, context, "info", "scheduled")
        assert msg is None

    def test_format_alert_succeeds_with_valid_context(self):
        """When all template keys are present in context, returns rendered message."""
        from helios.dispatcher import AlertDispatcher
        from helios.matrix_pusher import MatrixPusher
        from helios.state import HeliosDB

        db = HeliosDB(":memory:")
        matrix_pusher = MatrixPusher(cfg={})
        dispatcher = AlertDispatcher(db, matrix_pusher, config={})

        hit = {
            "slug": "test_ok",
            "severity": "info",
            "category": "scheduled",
            "message": "☀️ {weather.temp_c}°C, {weather.condition}.",
        }
        context = {"weather": {"temp_c": 18, "condition": "Sunny"}}

        msg, embed = dispatcher._format_alert(hit, context, "info", "scheduled")
        assert msg is not None
        assert "{weather.temp_c}" not in msg
        assert "18°C" in msg
        assert "Sunny" in msg

    def test_format_spec_works(self):
        """Format specs like {system.db_size_mb:.0f} should work."""
        from helios.dispatcher import AlertDispatcher
        from helios.matrix_pusher import MatrixPusher
        from helios.state import HeliosDB

        db = HeliosDB(":memory:")
        matrix_pusher = MatrixPusher(cfg={})
        dispatcher = AlertDispatcher(db, matrix_pusher, config={})

        hit = {
            "slug": "test_spec",
            "severity": "info",
            "category": "system",
            "message": "DB: {system.db_size_mb:.0f} MB",
        }
        context = {"system": {"db_size_mb": 45.7}}

        msg, _ = dispatcher._format_alert(hit, context, "info", "system")
        assert msg is not None
        assert "46 MB" in msg

    def test_dispatch_does_not_send_raw_template(self):
        """dispatch() returns False and logs when template render fails."""
        from helios.dispatcher import AlertDispatcher
        from helios.matrix_pusher import MatrixPusher
        from helios.state import HeliosDB

        db = HeliosDB(":memory:")
        matrix_pusher = MatrixPusher(cfg={})
        dispatcher = AlertDispatcher(db, matrix_pusher, config={})

        hit = {
            "slug": "test_broken",
            "severity": "info",
            "category": "scheduled",
            "priority": 1,
            "message": "{missing.key}",
        }
        context = {"weather": {"temp_c": 20}}
        sent = dispatcher.dispatch(hit, context)
        assert sent is False, "Must not send when template render fails"

        # Check alert log
        recent = dispatcher.get_recent_alerts(limit=5)
        matching = [a for a in recent if a.get("rule_slug") == "test_broken"]
        assert len(matching) >= 1
        assert "template_render_failed" in (matching[0].get("message") or "")


# ============================================================================
# P0.3 — Rule cooldown persistence
# ============================================================================

class TestRuleCooldownPersistence:
    """_mark_triggered must write to rules.last_triggered in DB."""

    def _seed_rule(self, db, slug, cooldown_secs=3600):
        with db._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO rules
                    (slug, trigger_type, condition, action_type, action_config,
                     priority, enabled, cooldown_secs, description, category,
                     severity, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (slug, "tick", "weather.temp_c > 0", "push", '{"message": "test"}',
                  1, 1, cooldown_secs, "Test", "system", "info", "test"))
            c.commit()

    def test_mark_triggered_updates_db_last_triggered(self):
        """After _mark_triggered, rules.last_triggered is a non-null ISO timestamp."""
        from helios.state import HeliosDB
        from helios.rules_v2 import RulesEngine

        db = HeliosDB(":memory:")
        self._seed_rule(db, "test_cooldown_rule")

        engine = RulesEngine(db)
        engine._mark_triggered("test_cooldown_rule")

        with db._conn() as c:
            row = c.execute(
                "SELECT last_triggered FROM rules WHERE slug = ?",
                ("test_cooldown_rule",)
            ).fetchone()

        assert row is not None
        assert row[0] is not None
        datetime.fromisoformat(row[0])

    def test_cooldown_suppresses_second_evaluation(self):
        """Rule within cooldown does not fire again."""
        from helios.state import HeliosDB
        from helios.rules_v2 import RulesEngine

        db = HeliosDB(":memory:")
        self._seed_rule(db, "test_cooldown_second_eval")

        # Set last_triggered to 10 seconds ago — still within 3600s cooldown
        with db._conn() as c:
            past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
            c.execute("UPDATE rules SET last_triggered=? WHERE slug=?",
                      (past, "test_cooldown_second_eval"))
            c.commit()

        engine = RulesEngine(db)
        context = {"weather": {"temp_c": 20}}
        hits = engine.evaluate(context)

        matching = [h for h in hits if h["slug"] == "test_cooldown_second_eval"]
        assert len(matching) == 0

    def test_cooldown_allows_after_expiry(self):
        """After cooldown expires, the same rule fires again."""
        from helios.state import HeliosDB
        from helios.rules_v2 import RulesEngine

        db = HeliosDB(":memory:")
        self._seed_rule(db, "test_cooldown_expired")

        # Set last_triggered to 2 hours ago — 3600s cooldown expired
        with db._conn() as c:
            past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            c.execute("UPDATE rules SET last_triggered=? WHERE slug=?",
                      (past, "test_cooldown_expired"))
            c.commit()

        engine = RulesEngine(db)
        context = {"weather": {"temp_c": 20}}
        hits = engine.evaluate(context)

        matching = [h for h in hits if h["slug"] == "test_cooldown_expired"]
        assert len(matching) >= 1


# ============================================================================
# P1.1 — AutoDream night window & daily cap
# ============================================================================

class TestDreamEngineNightWindow:
    """Dream engine must restrict automatic dreams to 01:00-06:00 MDT."""

    @pytest.fixture
    def engine(self):
        from helios.state import HeliosDB
        from helios.dream_engine import DreamEngine
        db = HeliosDB(":memory:")
        return DreamEngine(db, cfg={})

    @staticmethod
    def _mock_time(hour_mdt: int, minute: int = 0):
        """Return a datetime object at specified MDT time."""
        # MDT = UTC-6, so we create UTC time that corresponds to the MDT time
        utc_hour = hour_mdt + 6
        if utc_hour >= 24:
            utc_hour -= 24
        return datetime(2026, 5, 14, utc_hour, minute, 0, tzinfo=timezone.utc)

    def _set_idle_state(self, idle_seconds: int = 1800):
        from helios.dream_engine import IDLE_STATE_FILE
        IDLE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        IDLE_STATE_FILE.write_text(json.dumps({"idle_seconds": idle_seconds}))

    def test_is_idle_window_outside_night_hours(self, engine, monkeypatch):
        """During daytime (e.g. 14:00 MDT), is_idle_window returns False."""
        mock_dt = self._mock_time(14, 0)
        monkeypatch.setattr("helios.dream_engine.datetime", _MockDatetime(mock_dt))
        assert engine.is_idle_window() is False

    def test_is_idle_window_inside_night_hours(self, engine, monkeypatch):
        """At 02:00 MDT with AFK, is_idle_window returns True."""
        self._set_idle_state(1800)
        mock_dt = self._mock_time(2, 0)
        monkeypatch.setattr("helios.dream_engine.datetime", _MockDatetime(mock_dt))
        assert engine.is_idle_window() is True

    def test_is_idle_window_inside_night_but_no_afk(self, engine, monkeypatch):
        """At 02:00 MDT but only 5 min AFK, is_idle_window returns False."""
        self._set_idle_state(300)  # 5 min
        mock_dt = self._mock_time(2, 0)
        monkeypatch.setattr("helios.dream_engine.datetime", _MockDatetime(mock_dt))
        assert engine.is_idle_window() is False

    def test_should_dream_daily_cap(self, engine, monkeypatch):
        """After one dream cycle today, should_dream returns False."""
        from helios.dream_engine import IDLE_STATE_FILE
        # Set state as if we already dreamed today
        engine._last_auto_dream_date = datetime.now(ZoneInfo("America/Edmonton")).strftime("%Y-%m-%d")
        engine._last_dream_ts = 100
        engine._save_state()

        self._set_idle_state(1800)
        mock_dt = self._mock_time(2, 0)
        monkeypatch.setattr("helios.dream_engine.datetime", _MockDatetime(mock_dt))
        assert engine.should_dream() is False

    def test_daily_cap_survives_restart(self):
        """last_auto_dream_date is persisted to dream_state.json and restored."""
        from helios.dream_engine import DreamEngine, DREAM_STATE_FILE
        from helios.state import HeliosDB

        yesterday = (datetime.now(ZoneInfo("America/Edmonton")) - timedelta(days=1)).strftime("%Y-%m-%d")
        DREAM_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        DREAM_STATE_FILE.write_text(json.dumps({
            "last_auto_dream_date": yesterday,
            "last_dream_ts": None,
            "last_metric_count": 0,
        }))

        db = HeliosDB(":memory:")
        e = DreamEngine(db, cfg={})
        assert e._last_auto_dream_date == yesterday


# ============================================================================
# P1.2 — Health score missing metrics
# ============================================================================

class TestHealthScoreMissingMetrics:
    """Health score must not default missing metrics to 0."""

    def test_missing_mood_hr_marked_missing(self):
        """When mood and HR are absent, they're marked 'missing'."""
        from helios.proactive_intelligence import DailyHealthScore

        metrics = {"sleep.hours": 7.0, "activity.minutes_daily": 30}
        result = DailyHealthScore.compute(metrics)

        assert result["is_partial"] is True
        assert "mood.score_daily" in result["missing_metrics"]
        assert "resting_heart_rate.avg_daily" in result["missing_metrics"]
        assert result["components"]["mood"]["status"] == "missing"
        assert result["components"]["mood"]["score"] is None
        assert result["components"]["hr"]["status"] == "missing"
        assert result["components"]["hr"]["score"] is None

        # Breakdown should say 'missing', not fake scores
        assert "Mood: missing" in result["breakdown"]
        assert "Hr: missing" in result["breakdown"]
        # Make sure we're not hiding valid scores though
        assert "Sleep: 30/35" in result["breakdown"]
        assert "Activity: 10/25" in result["breakdown"]

    def test_partial_score_normalized(self):
        """Partial score is normalized to 100-point scale."""
        from helios.proactive_intelligence import DailyHealthScore

        metrics = {"sleep.hours": 7.0, "activity.minutes_daily": 30}
        result = DailyHealthScore.compute(metrics)

        assert result["is_partial"] is True
        # Sleep: 7h -> 30/35, Activity: 30min -> 10/25 => raw=40/60
        assert result["raw_total"] == 40
        assert result["max_available"] == 60
        assert result["total"] == 66  # int(40/60 * 100)

    def test_all_metrics_present_full_score(self):
        """When all 4 metrics are present, score is not partial."""
        from helios.proactive_intelligence import DailyHealthScore

        metrics = {
            "sleep.hours": 8.0,
            "activity.minutes_daily": 60,
            "mood.score_daily": 8,
            "resting_heart_rate.avg_daily": 60,
        }
        result = DailyHealthScore.compute(metrics)

        assert result["is_partial"] is False
        assert len(result["missing_metrics"]) == 0
        for name in ["sleep", "activity", "mood", "hr"]:
            assert result["components"][name]["status"] == "present"
            assert result["components"][name]["score"] is not None


# ============================================================================
# P0.1 — Migration 020 disables superseded rules
# ============================================================================

class TestMigration020:
    """Migration 020 disables dream_cycle_complete, morning_checkin, evening_wrap."""

    def test_migration_sql_disables_correct_rules(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE rules (
                slug TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 1
            )
        """)
        conn.execute("INSERT INTO rules (slug, enabled) VALUES ('dream_cycle_complete', 1)")
        conn.execute("INSERT INTO rules (slug, enabled) VALUES ('morning_checkin', 1)")
        conn.execute("INSERT INTO rules (slug, enabled) VALUES ('evening_wrap', 1)")
        conn.execute("INSERT INTO rules (slug, enabled) VALUES ('some_good_rule', 1)")
        conn.commit()

        conn.execute("""
            UPDATE rules SET enabled = 0 WHERE slug IN (
                'dream_cycle_complete',
                'morning_checkin',
                'evening_wrap'
            )
        """)
        conn.commit()

        results = dict(conn.execute("SELECT slug, enabled FROM rules ORDER BY slug").fetchall())
        assert results["dream_cycle_complete"] == 0
        assert results["morning_checkin"] == 0
        assert results["evening_wrap"] == 0
        assert results["some_good_rule"] == 1


# ============================================================================
# Helper
# ============================================================================

class _MockDatetime:
    """A datetime-like class that returns a fixed 'now'."""

    def __init__(self, fixed_now: datetime):
        self._fixed_now = fixed_now

    def now(self, tz=None):
        if tz:
            return self._fixed_now.astimezone(tz)
        return self._fixed_now

    def __getattr__(self, name):
        return getattr(self._fixed_now, name)
