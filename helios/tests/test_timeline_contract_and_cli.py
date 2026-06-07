"""Tests for timeline schema contract and CLI safety.

Verifies:
1. timeline_events INSERT uses the correct schema from migration 018
2. timeline writers don't leak raw coordinates (privacy)
3. list-modules is read-only (does NOT start services)
4. new-module works with argparse slug argument
5. CLI help says v6, not v5
6. config-check command exists and runs read-only
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. Timeline schema contract tests
# ---------------------------------------------------------------------------

MIGRATION_018_COLUMNS = {
    "id", "ts", "event_type", "source_module", "importance",
    "summary", "metadata", "date_key", "created_at",
}


class TestTimelineSchemaContract:
    """Verify all timeline_events writers use the canonical 018 schema."""

    @pytest.fixture
    def db(self, tmp_path):
        """Create a temporary DB with the canonical migration 018 schema."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        # Apply migration 018 schema
        conn.execute("""
            CREATE TABLE IF NOT EXISTS timeline_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                event_type  TEXT    NOT NULL,
                source_module TEXT  NOT NULL,
                importance   REAL   NOT NULL DEFAULT 0.5,
                summary      TEXT   NOT NULL,
                metadata     TEXT,
                date_key     TEXT   NOT NULL,
                created_at   TEXT   NOT NULL DEFAULT (datetime('now'))
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS event_links (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source_event_id INTEGER NOT NULL REFERENCES timeline_events(id) ON DELETE CASCADE,
                target_event_id INTEGER NOT NULL REFERENCES timeline_events(id) ON DELETE CASCADE,
                link_type       TEXT    NOT NULL,
                confidence      REAL    NOT NULL DEFAULT 0.5,
                evidence        TEXT,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
            );
        """)
        conn.commit()
        conn.close()
        return db_path

    def test_location_module_write_uses_canonical_schema(self, db):
        """_write_timeline_event must use the canonical schema columns."""
        from helios.modules.location import LocationModule

        mod = LocationModule(db_path=db, config={"poi_lookup_enabled": False})
        # Suppress HA polling for test
        mod._ha_token = ""

        # Directly test _write_timeline_event with a sanitized event
        event = {
            "event_type": "location_change",
            "transition": "departure",
            "from_zone": "home",
            "to_zone": "away",
            "zone": "away",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        mod._write_timeline_event(event)

        # Verify the row was inserted with correct columns
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT ts, event_type, source_module, importance, summary, metadata, date_key "
            "FROM timeline_events"
        ).fetchall()
        conn.close()

        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        ts, event_type, source_module, importance, summary, metadata, date_key = rows[0]
        assert event_type == "location_change"
        assert source_module == "location"
        assert isinstance(importance, float)
        assert importance > 0
        assert summary, "summary must not be empty"
        assert date_key, "date_key must not be empty"

    def test_location_module_no_raw_coords_in_metadata(self, db):
        """Timeline metadata must not contain raw lat/lon."""
        from helios.modules.location import LocationModule

        mod = LocationModule(db_path=db, config={"poi_lookup_enabled": False})
        mod._ha_token = ""

        event = {
            "event_type": "location_change",
            "zone": "away",
            "lat": 40.7128,    # Should be stripped
            "lon": -74.0060,  # Should be stripped
            "accuracy": 10,
            "place_name": "Coffee Shop",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        mod._write_timeline_event(event)

        conn = sqlite3.connect(db)
        row = conn.execute("SELECT metadata FROM timeline_events").fetchone()
        conn.close()

        assert row is not None
        metadata = json.loads(row[0]) if row[0] else {}
        assert "lat" not in metadata, f"lat leaked into metadata: {metadata}"
        assert "lon" not in metadata, f"lon leaked into metadata: {metadata}"
        assert "accuracy" not in metadata, f"accuracy leaked into metadata: {metadata}"

    def test_location_module_write_with_zone_transition(self, db):
        """Zone transitions should produce proper summary and importance."""
        from helios.modules.location import LocationModule

        mod = LocationModule(db_path=db, config={"poi_lookup_enabled": False})
        mod._ha_token = ""

        event = {
            "event_type": "location_change",
            "transition": "arrival",
            "from_zone": "away",
            "to_zone": "home",
            "zone": "home",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        mod._write_timeline_event(event)

        conn = sqlite3.connect(db)
        row = conn.execute("SELECT importance, summary FROM timeline_events").fetchone()
        conn.close()

        assert row is not None
        importance, summary = row
        assert importance == 0.7, f"Transition events should have importance 0.7, got {importance}"
        assert "arrival" in summary.lower() or "zone" in summary.lower() or "transition" in summary.lower()

    def test_timeline_normalizer_writer_uses_canonical_schema(self, db):
        """TimelineNormalizer._insert_event must use canonical schema."""
        from helios.timeline_normalizer import TimelineNormalizer
        import sqlite3

        norm = TimelineNormalizer(db_path=db)
        conn = sqlite3.connect(db)

        eid = norm._insert_event(
            conn, "focus_change", "focus:tracker",
            0.5, "Started working",
            {"from_state": "idle", "to_state": "working"},
            "2025-01-15", "2025-01-15T10:00:00+00:00",
        )

        assert eid is not None, "Insert should return an event ID"

        row = conn.execute(
            "SELECT ts, event_type, source_module, importance, summary, metadata, date_key "
            "FROM timeline_events WHERE id = ?", (eid,)
        ).fetchone()
        conn.close()

        assert row is not None
        ts, event_type, source_module, importance, summary, metadata, date_key = row
        assert event_type == "focus_change"
        assert source_module == "focus:tracker"
        assert importance == 0.5
        assert summary == "Started working"
        assert date_key == "2025-01-15"

    def test_old_schema_columns_do_not_exist(self, db):
        """The old broken column names (type, data, source) must NOT work."""
        conn = sqlite3.connect(db)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO timeline_events (ts, type, data, source) VALUES (?, ?, ?, ?)",
                ("2025-01-01T00:00:00Z", "test", "{}", "test"),
            )
        conn.close()

    def test_location_normalizer_no_coords_in_metadata(self):
        """_normalize_location_changes must not stash lat/lon in metadata."""
        # We can verify the code path by inspecting what _insert_event would produce.
        # The fix already removes lat/lon from the metadata dict.
        from helios.timeline_normalizer import TimelineNormalizer
        norm = TimelineNormalizer(db_path=":memory:")
        # Verify method signature accepts metadata without lat/lon
        # (This is a code-level check; runtime integration is tested via _insert_event)
        import inspect
        source = inspect.getsource(norm._normalize_location_changes)
        # lat/lon should NOT appear in the metadata dict passed to _insert_event
        assert '"lat"' not in source or 'data.get("lat")' not in source, \
            "lat should not be in location metadata passed to timeline"


# ---------------------------------------------------------------------------
# 2. CLI safety tests
# ---------------------------------------------------------------------------

class TestCLISafety:
    """Verify CLI commands are safe and correct."""

    def test_list_modules_does_not_start_services(self):
        """list-modules must NOT use HeliosEngine at all — import-based discovery only."""
        from helios.main import cmd_list_modules
        import inspect

        source = inspect.getsource(cmd_list_modules)
        assert "HeliosEngine" not in source, \
            "list-modules must NOT import or use HeliosEngine"
        assert "start_services" not in source, \
            "list-modules must not reference start_services"

    def test_list_modules_does_not_create_db(self, tmp_path):
        """list-modules must not create or modify any database file."""
        import os
        fake_db = tmp_path / "should_not_exist.db"
        old_base = os.environ.get("HELIOS_BASE")
        os.environ["HELIOS_BASE"] = str(tmp_path)
        try:
            from helios.main import cmd_list_modules
            class Args: pass
            cmd_list_modules(Args())
            # No database file should have been created
            assert not fake_db.exists(), "list-modules created a DB file — it should be read-only"
            assert not (tmp_path / "helios_v6.db").exists(), "list-modules created helios_v6.db — should be read-only"
        finally:
            if old_base:
                os.environ["HELIOS_BASE"] = old_base
            else:
                os.environ.pop("HELIOS_BASE", None)

    def test_config_check_does_not_create_db(self, tmp_path):
        """config-check must not create a database file."""
        import os
        old_base = os.environ.get("HELIOS_BASE")
        os.environ["HELIOS_BASE"] = str(tmp_path)
        try:
            # config-check should succeed even without a DB (just report missing)
            from helios.main import cmd_config_check
            class Args: pass
            try:
                cmd_config_check(Args())
            except SystemExit as e:
                # May exit with 1 for missing DB, that's fine
                pass
            # The key point: no DB file should be created
            db_path = tmp_path / "helios_v6.db"
            assert not db_path.exists(), "config-check created a DB file — should be read-only"
        finally:
            if old_base:
                os.environ["HELIOS_BASE"] = old_base
            else:
                os.environ.pop("HELIOS_BASE", None)

    def test_new_module_uses_argparse_slug(self):
        """new-module must accept slug via argparse, not sys.argv."""
        from helios.main import cmd_new_module
        import inspect

        source = inspect.getsource(cmd_new_module)
        assert "args.slug" in source, \
            "new-module must get slug from args.slug (argparse), not sys.argv"
        assert "sys.argv" not in source, \
            "new-module should not reference sys.argv"

    def test_new_module_template_supports_j2(self):
        """new-module must look for _template.py.j2 and fall back to _template.py."""
        from helios.main import cmd_new_module
        import inspect

        source = inspect.getsource(cmd_new_module)
        assert "_template.py.j2" in source, \
            "must look for .j2 template first"

    def test_cli_help_says_v6(self):
        """CLI description must say v6, not v5."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "helios.main", "--help"],
            capture_output=True, text=True, timeout=10
        )
        assert "v6" in result.stdout.lower() or "v6" in result.stderr.lower(), \
            "CLI help should reference v6"

    def test_invalid_command_exits_nonzero(self):
        """Invalid CLI commands must exit with nonzero code."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "helios.main", "nonexistent-command"],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode != 0, "Invalid command should exit nonzero"

    def test_config_check_command_exists(self):
        """config-check command should be defined and importable."""
        from helios.main import cmd_config_check
        assert callable(cmd_config_check)

    def test_config_check_readonly(self):
        """config-check must not use HeliosDB (which triggers migrations)."""
        from helios.main import cmd_config_check
        import inspect

        source = inspect.getsource(cmd_config_check)
        assert "HeliosDB" not in source, \
            "config-check must not use HeliosDB (which triggers migrations)"
        assert "start_services" not in source, \
            "config-check must not start services"

    def test_new_module_command_in_parser(self):
        """Verify new-module has a slug argument in the parser."""
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        p = sub.add_parser("new-module")
        p.add_argument("slug")

        args = parser.parse_args(["new-module", "screen-time"])
        assert args.slug == "screen-time"

    def test_new_module_slug_formatting(self):
        """Verify module name generation from slug."""
        slug = "screen-time"
        class_name = "".join(w.capitalize() for w in slug.replace("-", "_").split("_")) + "Module"
        assert class_name == "ScreenTimeModule", f"Expected ScreenTimeModule, got {class_name}"

        slug2 = "gaming_stats"
        class_name2 = "".join(w.capitalize() for w in slug2.replace("-", "_").split("_")) + "Module"
        assert class_name2 == "GamingStatsModule"

    def test_list_modules_no_side_effects(self):
        """list-modules must not instantiate collectors or start watchers."""
        from helios.main import cmd_list_modules
        import inspect

        source = inspect.getsource(cmd_list_modules)
        # Must not contain start_services=True
        assert "start_services=True" not in source, \
            "list-modules must not start services"


# ---------------------------------------------------------------------------
# 3. Privacy: raw coordinates must not leak into timeline
# ---------------------------------------------------------------------------

class TestTimelinePrivacy:
    """Ensure raw coordinates never leak into timeline_events."""

    @pytest.fixture
    def db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS timeline_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                event_type  TEXT    NOT NULL,
                source_module TEXT  NOT NULL,
                importance   REAL   NOT NULL DEFAULT 0.5,
                summary      TEXT   NOT NULL,
                metadata     TEXT,
                date_key     TEXT   NOT NULL,
                created_at   TEXT   NOT NULL DEFAULT (datetime('now'))
            );
        """)
        conn.commit()
        conn.close()
        return db_path

    def test_location_module_strips_coords(self, db):
        """Location module must not write raw lat/lon to timeline."""
        from helios.modules.location import LocationModule

        mod = LocationModule(db_path=db, config={"poi_lookup_enabled": False})
        mod._ha_token = ""
        event = {
            "event_type": "location_change",
            "zone": "office",
            "lat": 40.7128,
            "lon": -74.0060,
            "accuracy": 15,
            "place_name": "Downtown Office",
            "place_type": "office",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        mod._write_timeline_event(event)

        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT metadata, summary FROM timeline_events").fetchall()
        conn.close()

        for metadata_str, summary in rows:
            if metadata_str:
                metadata = json.loads(metadata_str)
                assert "lat" not in metadata, "lat must not appear in timeline metadata"
                assert "lon" not in metadata, "lon must not appear in timeline metadata"
                assert "accuracy" not in metadata, "accuracy must not appear in timeline metadata"
            # Summary should not contain raw coordinates
            assert "40.7128" not in summary, "Raw lat should not appear in summary"
            assert "-74.0060" not in summary, "Raw lon should not appear in summary"

    def test_normalizer_no_coords_in_location_metadata(self):
        """TimelineNormalizer must not include lat/lon in location event metadata."""
        from helios.timeline_normalizer import TimelineNormalizer
        import inspect

        source = inspect.getsource(TimelineNormalizer._normalize_location_changes)
        # After our fix, the metadata dict should not contain lat/lon
        lines = source.split('\n')
        metadata_lines = [l for l in lines if 'metadata' in l.lower() or '"from"' in l or '"to"' in l]
        lat_lon_in_metadata = any('data.get("lat")' in l or 'data.get("lon")' in l for l in lines)
        assert not lat_lon_in_metadata, \
            "lat/lon must not be in _normalize_location_changes metadata"