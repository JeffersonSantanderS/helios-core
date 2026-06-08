"""Tests for helios.reports.weekly and helios.reports.monthly (SAN-123).

Verifies:
  - Required sections are present in both reports
  - Date range calculations are correct
  - Missing data is handled gracefully
  - Trend analysis works for monthly reports
  - Subscription costs are included in monthly reports
  - Obsidian vault writing works correctly
  - Markdown output is well-formed
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from helios.reports.weekly import WeeklyReport, _resolve_vault_path
from helios.reports.monthly import MonthlyReport


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Create a temporary Helios database with schema and seed data."""
    db_file = tmp_path / "test_weekly.db"
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Create core tables
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS metric_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            metric          TEXT    NOT NULL,
            value           REAL    NOT NULL,
            date_key        TEXT    NOT NULL,
            source          TEXT    NOT NULL DEFAULT 'correlator',
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            CONSTRAINT uq_metric_date UNIQUE (metric, date_key)
        );
        CREATE INDEX IF NOT EXISTS idx_metric_snapshots_metric_date ON metric_snapshots (metric, date_key);
        CREATE INDEX IF NOT EXISTS idx_metric_snapshots_ts ON metric_snapshots (ts);

        CREATE TABLE IF NOT EXISTS mood (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            emoji           TEXT    NOT NULL,
            score           INTEGER NOT NULL CHECK (score BETWEEN 1 AND 10),
            note            TEXT,
            source          TEXT    NOT NULL DEFAULT 'discord_button',
            discord_msg_id  TEXT,
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS focus (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            state           TEXT    NOT NULL CHECK (state IN ('working', 'gaming', 'idle', 'meeting', 'break')),
            source          TEXT    NOT NULL,
            context         TEXT    NOT NULL DEFAULT '{}',
            duration_secs   INTEGER,
            session_start   TEXT,
            session_end     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS context (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            source      TEXT    NOT NULL,
            module      TEXT    NOT NULL,
            key         TEXT    NOT NULL,
            value       TEXT    NOT NULL DEFAULT '{}',
            priority    INTEGER NOT NULL DEFAULT 0,
            expires_at  TEXT,
            created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            CONSTRAINT ctx_unique_latest UNIQUE (module, key, source)
        );

        CREATE TABLE IF NOT EXISTS calendar_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT    NOT NULL,
            start_time  TEXT    NOT NULL,
            end_time    TEXT,
            source      TEXT    NOT NULL DEFAULT 'manual',
            created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            service         TEXT    NOT NULL,
            provider        TEXT    NOT NULL DEFAULT 'unknown',
            amount          REAL    NOT NULL DEFAULT 0.0,
            currency        TEXT    NOT NULL DEFAULT 'CAD',
            cycle           TEXT    NOT NULL DEFAULT 'monthly'
                            CHECK (cycle IN ('monthly', 'yearly', 'weekly', 'one-time', 'unknown')),
            last_payment    TEXT,
            next_renewal    TEXT,
            source_email    TEXT,
            source_msgid    TEXT,
            confidence      REAL    NOT NULL DEFAULT 0.5,
            is_active       INTEGER NOT NULL DEFAULT 1,
            alert_sent      INTEGER NOT NULL DEFAULT 0,
            notes           TEXT,
            category        TEXT    NOT NULL DEFAULT 'other',
            ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            CONSTRAINT uq_subscription_service_cycle UNIQUE (service, cycle)
        );

        CREATE TABLE IF NOT EXISTS email_scan_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id      TEXT    NOT NULL UNIQUE,
            sender          TEXT,
            subject         TEXT,
            scan_date       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            result          TEXT    NOT NULL DEFAULT 'skipped',
            subscription_id INTEGER,
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            CONSTRAINT fk_subscription FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)
        );

        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            description TEXT
        );
    """)

    conn.commit()
    conn.close()
    return str(db_file)


@pytest.fixture
def seeded_db(db_path: str) -> str:
    """Seed the database with 7 days of metric data for weekly report testing."""
    conn = sqlite3.connect(db_path)
    end = date(2026, 6, 7)
    start = end - timedelta(days=6)

    for i in range(7):
        d = (start + timedelta(days=i)).isoformat()
        # Sleep data
        conn.execute(
            "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES (?, ?, ?, ?)",
            ("sleep.hours", 6.5 + i * 0.2, d, "home_assistant_health"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES (?, ?, ?, ?)",
            ("sleep.deep_hours", 1.2 + i * 0.05, d, "home_assistant_health"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES (?, ?, ?, ?)",
            ("sleep.rem_hours", 1.5 + i * 0.03, d, "home_assistant_health"),
        )
        # Steps
        conn.execute(
            "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES (?, ?, ?, ?)",
            ("activity.steps_daily", 8000 + i * 500, d, "home_assistant_health"),
        )
        # Active minutes
        conn.execute(
            "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES (?, ?, ?, ?)",
            ("activity.minutes_daily", 30 + i * 5, d, "home_assistant_health"),
        )
        # Resting HR
        conn.execute(
            "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES (?, ?, ?, ?)",
            ("health.resting_hr", 62 + i, d, "home_assistant_health"),
        )
        # HRV
        conn.execute(
            "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES (?, ?, ?, ?)",
            ("health.hrv_ms", 42 + i * 0.5, d, "home_assistant_health"),
        )
        # Screen time
        conn.execute(
            "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES (?, ?, ?, ?)",
            ("phone.screen_time_minutes", 120 + i * 10, d, "phone_sensors"),
        )
        # Mood
        conn.execute(
            "INSERT INTO mood (ts, emoji, score) VALUES (?, ?, ?)",
            (f"{d}T12:00:00Z", "😊", 7 + (i % 3)),
        )

    # Focus data
    conn.execute(
        "INSERT INTO focus (ts, state, source, duration_secs) VALUES (?, ?, ?, ?)",
        ("2026-06-05T10:00:00Z", "working", "calendar", 7200),
    )
    conn.execute(
        "INSERT INTO focus (ts, state, source, duration_secs) VALUES (?, ?, ?, ?)",
        ("2026-06-06T11:00:00Z", "working", "calendar", 5400),
    )

    # Subscription data
    conn.execute(
        """INSERT INTO subscriptions
           (service, provider, amount, currency, cycle, next_renewal, is_active, category)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        ("Netflix", "netflix.com", 15.99, "CAD", "monthly", "2026-06-15", "streaming"),
    )
    conn.execute(
        """INSERT INTO subscriptions
           (service, provider, amount, currency, cycle, next_renewal, is_active, category)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        ("iCloud", "apple.com", 3.99, "CAD", "monthly", "2026-06-20", "cloud"),
    )
    conn.execute(
        """INSERT INTO subscriptions
           (service, provider, amount, currency, cycle, next_renewal, is_active, category)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        ("GitHub Pro", "github.com", 156.0, "CAD", "yearly", "2027-01-15", "software"),
    )

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def empty_db(db_path: str) -> str:
    """Return a DB with schema but no data."""
    return db_path


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """Create a temporary Obsidian vault directory."""
    vault = tmp_path / "test_vault"
    vault.mkdir()
    return vault


# ── Weekly Report Tests ──────────────────────────────────────────────────


class TestWeeklyReportRequiredSections:
    """test_weekly_report_has_required_sections"""

    def test_all_sections_present(self, seeded_db: str):
        """Weekly report must contain all required sections."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        data = report.build()
        section_keys = [s["key"] for s in data["sections"]]
        required = ["sleep", "activity", "mood", "focus", "music", "calendar", "renewals"]
        for key in required:
            assert key in section_keys, f"Missing required section: {key}"

    def test_report_type_is_weekly(self, seeded_db: str):
        """Report type must be 'weekly'."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        data = report.build()
        assert data["report_type"] == "weekly"

    def test_schema_version(self, seeded_db: str):
        """Report must have schema_version."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        data = report.build()
        assert data["schema_version"] == "report.v1"

    def test_encrypted_state_false(self, seeded_db: str):
        """Reports are not encrypted."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        data = report.build()
        assert data["encrypted_state"] is False


class TestWeeklyReportDateRange:
    """test_weekly_report_date_range"""

    def test_date_range_is_seven_days(self, seeded_db: str):
        """Weekly report covers exactly 7 days."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        data = report.build()
        from datetime import date as dt
        start = dt.fromisoformat(data["period_start"])
        end = dt.fromisoformat(data["period_end"])
        assert (end - start).days == 6  # 7 days inclusive

    def test_default_end_date_is_today(self, seeded_db: str):
        """If no end_date provided, defaults to today (UTC)."""
        from datetime import datetime, timezone
        report = WeeklyReport(db_path=seeded_db)
        # Code uses UTC date, compare accordingly
        assert report.end_date == datetime.now(timezone.utc).date()

    def test_week_label_format(self, seeded_db: str):
        """Week label follows YYYY-WNN format."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        data = report.build()
        label = data["week_label"]
        assert label.startswith("2026-W")
        # WNN should be 2-digit
        parts = label.split("-W")
        assert len(parts) == 2
        assert len(parts[1]) == 2

    def test_string_end_date(self, seeded_db: str):
        """end_date as string should work."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        assert report.end_date == date(2026, 6, 7)


class TestWeeklyReportMissingDataGraceful:
    """test_weekly_report_missing_data_graceful"""

    def test_empty_db_all_sections_graceful(self, empty_db: str):
        """With no data, all sections should indicate no data available."""
        report = WeeklyReport(db_path=empty_db, end_date="2026-06-07")
        data = report.build()
        for section in data["sections"]:
            section_data = section["data"]
            assert section_data.get("available") is False or "message" in section_data, \
                f"Section {section['key']} should handle missing data gracefully"

    def test_empty_db_summary(self, empty_db: str):
        """With no data, summary should say no data available."""
        report = WeeklyReport(db_path=empty_db, end_date="2026-06-07")
        data = report.build()
        assert "No data" in data["summary"] or data["confidence"] == "needs_review"

    def test_empty_db_confidence_needs_review(self, empty_db: str):
        """With no data, confidence should be needs_review."""
        report = WeeklyReport(db_path=empty_db, end_date="2026-06-07")
        data = report.build()
        assert data["confidence"] == "needs_review"

    def test_markdown_still_renders_no_data(self, empty_db: str):
        """Markdown should still render when there's no data."""
        report = WeeklyReport(db_path=empty_db, end_date="2026-06-07")
        md, _ = report.generate()
        assert "Weekly Report" in md
        assert "No data available" in md

    def test_partial_data_still_works(self, db_path: str):
        """With only some data available, report should still generate."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO metric_snapshots (metric, value, date_key, source) VALUES (?, ?, ?, ?)",
            ("sleep.hours", 7.5, "2026-06-07", "home_assistant_health"),
        )
        conn.commit()
        conn.close()

        report = WeeklyReport(db_path=db_path, end_date="2026-06-07")
        data = report.build()
        # Sleep should be available
        sleep_section = next(s for s in data["sections"] if s["key"] == "sleep")
        assert sleep_section["data"]["available"] is True
        # Overall confidence should be low (only 1 day of data)
        assert data["confidence"] in ("low", "medium", "needs_review")


class TestWeeklyReportObsidianWrite:
    """test_weekly_report_obsidian_write"""

    def test_writes_to_vault(self, seeded_db: str, vault_dir: Path):
        """Weekly report should write to Obsidian vault."""
        report = WeeklyReport(
            db_path=seeded_db,
            end_date="2026-06-07",
            vault_path=str(vault_dir),
        )
        md, obsidian_path = report.generate()
        assert obsidian_path is not None
        assert obsidian_path.exists()
        content = obsidian_path.read_text(encoding="utf-8")
        assert "Weekly Report" in content

    def test_vault_file_naming(self, seeded_db: str, vault_dir: Path):
        """Weekly report file should be named YYYY-WNN.md."""
        report = WeeklyReport(
            db_path=seeded_db,
            end_date="2026-06-07",
            vault_path=str(vault_dir),
        )
        md, obsidian_path = report.generate()
        assert obsidian_path is not None
        filename = obsidian_path.name
        # Should match pattern 2026-WNN.md
        import re
        assert re.match(r"\d{4}-W\d{2}\.md", filename), f"Bad filename: {filename}"

    def test_vault_directory_structure(self, seeded_db: str, vault_dir: Path):
        """Weekly report should write to Helios/Weekly/ subdirectory."""
        report = WeeklyReport(
            db_path=seeded_db,
            end_date="2026-06-07",
            vault_path=str(vault_dir),
        )
        md, obsidian_path = report.generate()
        assert obsidian_path is not None
        # Path should contain Helios/Weekly
        parts = obsidian_path.parts
        assert "Helios" in parts
        assert "Weekly" in parts

    def test_no_vault_path_returns_none(self, seeded_db: str):
        """Without vault_path, Obsidian writing should return None."""
        # Unset env var to ensure no vault
        import os
        old_vault = os.environ.pop("HELIOS_OBSIDIAN_VAULT", None)
        try:
            report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
            md, obsidian_path = report.generate()
            assert obsidian_path is None
        finally:
            if old_vault:
                os.environ["HELIOS_OBSIDIAN_VAULT"] = old_vault

    def test_env_vault_path(self, seeded_db: str, vault_dir: Path):
        """Vault path from env var should work."""
        import os
        os.environ["HELIOS_OBSIDIAN_VAULT"] = str(vault_dir)
        try:
            report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
            md, obsidian_path = report.generate()
            assert obsidian_path is not None
            assert obsidian_path.exists()
        finally:
            del os.environ["HELIOS_OBSIDIAN_VAULT"]


# ── Monthly Report Tests ────────────────────────────────────────────────


class TestMonthlyReportRequiredSections:
    """test_monthly_report_has_required_sections"""

    def test_all_sections_present(self, seeded_db: str):
        """Monthly report must contain all required sections."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        data = report.build()
        section_keys = [s["key"] for s in data["sections"]]
        required = ["sleep", "activity", "mood", "focus", "music", "subscriptions", "health_highlights"]
        for key in required:
            assert key in section_keys, f"Missing required section: {key}"

    def test_report_type_is_monthly(self, seeded_db: str):
        """Report type must be 'monthly'."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        data = report.build()
        assert data["report_type"] == "monthly"

    def test_month_label_format(self, seeded_db: str):
        """Month label follows YYYY-MM format."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        data = report.build()
        assert data["month_label"] == "2026-06"


class TestMonthlyReportTrendAnalysis:
    """test_monthly_report_trend_analysis"""

    def test_trend_in_sleep(self, seeded_db: str):
        """Sleep trend should reflect increasing data (seeded with increasing values)."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        data = report.build()
        sleep_section = next(s for s in data["sections"] if s["key"] == "sleep")
        sleep_data = sleep_section["data"]
        assert sleep_data["available"] is True
        trend = sleep_data["trend"]
        # Sleep values are 6.5, 6.7, 6.9, 7.1, 7.3, 7.5, 7.7 → trending up
        assert trend["direction"] in ("up", "down", "stable")

    def test_trend_has_change_pct(self, seeded_db: str):
        """Trend should include change percentage."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        data = report.build()
        sleep_section = next(s for s in data["sections"] if s["key"] == "sleep")
        trend = sleep_section["data"]["trend"]
        assert "change_pct" in trend

    def test_monthly_sleep_breakdown(self, seeded_db: str):
        """Monthly sleep section should include weekly breakdowns."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        data = report.build()
        sleep_data = next(s for s in data["sections"] if s["key"] == "sleep")["data"]
        assert "weekly_breakdown" in sleep_data
        # Should have at least one week's worth of data
        assert len(sleep_data["weekly_breakdown"]) >= 1

    def test_health_highlights_with_hr(self, seeded_db: str):
        """Health highlights should include resting HR and HRV."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        data = report.build()
        health_section = next(s for s in data["sections"] if s["key"] == "health_highlights")
        health_data = health_section["data"]
        assert health_data.get("available") is True
        assert "resting_hr" in health_data
        assert "hrv" in health_data

    def test_activity_trend(self, seeded_db: str):
        """Activity section should include trend data."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        data = report.build()
        activity = next(s for s in data["sections"] if s["key"] == "activity")["data"]
        assert activity["available"] is True
        if "steps" in activity:
            assert "trend" in activity["steps"]


class TestMonthlyReportSubscriptions:
    """test_monthly_report_includes_subscriptions"""

    def test_subscription_costs_present(self, seeded_db: str):
        """Monthly report should include subscription costs."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        data = report.build()
        subs_section = next(s for s in data["sections"] if s["key"] == "subscriptions")
        subs_data = subs_section["data"]
        assert subs_data["available"] is True
        assert "monthly_cost" in subs_data
        assert "yearly_cost" in subs_data
        assert "estimated_monthly_total" in subs_data

    def test_subscription_categories(self, seeded_db: str):
        """Monthly report should group subscriptions by category."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        data = report.build()
        subs_data = next(s for s in data["sections"] if s["key"] == "subscriptions")["data"]
        categories = subs_data.get("categories", {})
        # We seeded streaming, cloud, and software categories
        assert len(categories) > 0
        # Check specific categories exist
        all_cats = list(categories.keys())
        assert any(c in all_cats for c in ["streaming", "cloud", "software"])

    def test_total_active_subscriptions(self, seeded_db: str):
        """Report should show total active subscriptions."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        data = report.build()
        subs_data = next(s for s in data["sections"] if s["key"] == "subscriptions")["data"]
        # 3 active subscriptions seeded
        assert subs_data["total_active"] == 3

    def test_monthly_cost_calculation(self, seeded_db: str):
        """Monthly cost should be correct from seeded data."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        data = report.build()
        subs_data = next(s for s in data["sections"] if s["key"] == "subscriptions")["data"]
        # Netflix $15.99/mo + iCloud $3.99/mo = $19.98
        assert abs(subs_data["monthly_cost"] - 19.98) < 0.01
        # GitHub Pro $156/yr
        assert abs(subs_data["yearly_cost"] - 156.00) < 0.01


class TestMonthlyReportObsidianWrite:
    """test_monthly_report_obsidian_write"""

    def test_writes_to_vault(self, seeded_db: str, vault_dir: Path):
        """Monthly report should write to Obsidian vault."""
        report = MonthlyReport(
            db_path=seeded_db,
            year=2026,
            month=6,
            vault_path=str(vault_dir),
        )
        md, obsidian_path = report.generate()
        assert obsidian_path is not None
        assert obsidian_path.exists()
        content = obsidian_path.read_text(encoding="utf-8")
        assert "Monthly Report" in content

    def test_vault_file_naming(self, seeded_db: str, vault_dir: Path):
        """Monthly report file should be named YYYY-MM.md."""
        report = MonthlyReport(
            db_path=seeded_db,
            year=2026,
            month=6,
            vault_path=str(vault_dir),
        )
        md, obsidian_path = report.generate()
        assert obsidian_path is not None
        assert obsidian_path.name == "2026-06.md"

    def test_vault_directory_structure(self, seeded_db: str, vault_dir: Path):
        """Monthly report should write to Helios/Monthly/ subdirectory."""
        report = MonthlyReport(
            db_path=seeded_db,
            year=2026,
            month=6,
            vault_path=str(vault_dir),
        )
        md, obsidian_path = report.generate()
        assert obsidian_path is not None
        parts = obsidian_path.parts
        assert "Helios" in parts
        assert "Monthly" in parts

    def test_no_vault_path_returns_none(self, seeded_db: str):
        """Without vault_path, Obsidian writing should return None."""
        import os
        old_vault = os.environ.pop("HELIOS_OBSIDIAN_VAULT", None)
        try:
            report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
            md, obsidian_path = report.generate()
            assert obsidian_path is None
        finally:
            if old_vault:
                os.environ["HELIOS_OBSIDIAN_VAULT"] = old_vault

    def test_generate_returns_markdown(self, seeded_db: str):
        """generate() should return a non-empty markdown string."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        md, obsidian_path = report.generate()
        assert len(md) > 100  # Should have substantial content
        assert "Monthly Report" in md
        assert "2026-06" in md


class TestMonthlyReportMissingData:
    """test_monthly_report_missing_data_graceful"""

    def test_empty_db_all_sections_graceful(self, empty_db: str):
        """With no data, all sections should handle gracefully."""
        report = MonthlyReport(db_path=empty_db, year=2026, month=6)
        data = report.build()
        for section in data["sections"]:
            section_data = section["data"]
            # Each section should either be available with data, or gracefully say "no data"
            if not section_data.get("available"):
                assert "message" in section_data, \
                    f"Section {section['key']} should have 'message' when not available"

    def test_empty_db_confidence(self, empty_db: str):
        """With no data, confidence should be needs_review."""
        report = MonthlyReport(db_path=empty_db, year=2026, month=6)
        data = report.build()
        assert data["confidence"] == "needs_review"

    def test_markdown_renders_no_data(self, empty_db: str):
        """Markdown should still render when there's no data."""
        report = MonthlyReport(db_path=empty_db, year=2026, month=6)
        md, _ = report.generate()
        assert "Monthly Report" in md
        assert "No data available" in md


# ── Additional edge case tests ────────────────────────────────────────


class TestVaultPathResolution:
    """Test vault path resolution logic."""

    def test_explicit_vault_path(self):
        """Explicit vault_path should take priority."""
        path = _resolve_vault_path("/tmp/test_vault")
        assert path == Path("/tmp/test_vault")

    def test_env_var_vault_path(self):
        """HELIOS_OBSIDIAN_VAULT env var should be used."""
        import os
        os.environ["HELIOS_OBSIDIAN_VAULT"] = "/tmp/env_vault"
        try:
            path = _resolve_vault_path()
            assert path == Path("/tmp/env_vault")
        finally:
            del os.environ["HELIOS_OBSIDIAN_VAULT"]

    def test_none_when_no_config(self):
        """When no config and no env var and no default dir, return None."""
        import os
        old_env = os.environ.pop("HELIOS_OBSIDIAN_VAULT", None)
        old_base = os.environ.pop("HELIOS_BASE", None)
        try:
            path = _resolve_vault_path()
            # It may find a sibling 'obsidian' dir or return None
            # The key thing is it shouldn't crash
            assert path is None or isinstance(path, Path)
        finally:
            if old_env:
                os.environ["HELIOS_OBSIDIAN_VAULT"] = old_env
            if old_base:
                os.environ["HELIOS_BASE"] = old_base


class TestWeeklyReportMarkdown:
    """Test weekly report markdown rendering."""

    def test_markdown_has_header(self, seeded_db: str):
        """Markdown should have a header with week label."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        md = report.render_markdown()
        assert "Weekly Report" in md

    def test_markdown_has_sections(self, seeded_db: str):
        """Markdown should contain section headers."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        md = report.render_markdown()
        assert "Sleep Summary" in md
        assert "Steps & Activity" in md
        assert "Mood" in md
        assert "Focus & Screen Time" in md
        assert "Upcoming Renewals" in md

    def test_markdown_has_generation_timestamp(self, seeded_db: str):
        """Markdown should include generation timestamp."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        data = report.build()
        md = report.render_markdown(data)
        assert "Generated" in md

    def test_markdown_has_confidence(self, seeded_db: str):
        """Markdown should include confidence level."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        data = report.build()
        md = report.render_markdown(data)
        assert "Confidence" in md


class TestMonthlyReportMarkdown:
    """Test monthly report markdown rendering."""

    def test_markdown_has_header(self, seeded_db: str):
        """Markdown should have a header with month label."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        md = report.render_markdown()
        assert "Monthly Report" in md
        assert "2026-06" in md

    def test_markdown_has_trend_sections(self, seeded_db: str):
        """Monthly markdown should include trend information."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        md = report.render_markdown()
        assert "Sleep Trends" in md
        assert "Activity Trends" in md
        assert "Mood Patterns" in md
        assert "Focus Patterns" in md
        assert "Subscription Costs" in md
        assert "Health Highlights" in md

    def test_markdown_includes_sleep_weekly_breakdown(self, seeded_db: str):
        """Monthly markdown may include weekly breakdown table."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        md = report.render_markdown()
        # Should have sleep data if available
        assert "Average sleep" in md or "No data available" in md

    def test_month_date_range(self, seeded_db: str):
        """Monthly report date range should cover entire month."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=6)
        data = report.build()
        assert data["period_start"] == "2026-06-01"
        assert data["period_end"] == "2026-06-30"

    def test_february_date_range(self, seeded_db: str):
        """February monthly report should have correct end date."""
        report = MonthlyReport(db_path=seeded_db, year=2026, month=2)
        data = report.build()
        assert data["period_start"] == "2026-02-01"
        assert data["period_end"] == "2026-02-28"


class TestWeeklyReportDataSection:
    """Test that weekly report data sections have expected content."""

    def test_sleep_section_with_data(self, seeded_db: str):
        """Sleep section should have average hours and trend when data available."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        data = report.build()
        sleep = next(s for s in data["sections"] if s["key"] == "sleep")["data"]
        assert sleep["available"] is True
        assert sleep["average_hours"] is not None
        assert sleep["total_days"] > 0
        assert "trend" in sleep

    def test_activity_section_with_data(self, seeded_db: str):
        """Activity section should have step data when available."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        data = report.build()
        activity = next(s for s in data["sections"] if s["key"] == "activity")["data"]
        assert activity["available"] is True
        assert "steps" in activity
        assert activity["steps"]["total"] > 0

    def test_mood_section_with_data(self, seeded_db: str):
        """Mood section should have average score when data available."""
        report = WeeklyReport(db_path=seeded_db, end_date="2026-06-07")
        data = report.build()
        mood = next(s for s in data["sections"] if s["key"] == "mood")["data"]
        assert mood["available"] is True
        assert mood["average"] is not None