"""Helios v5 — Test State: migration registry and schema integrity."""

import os
import re
import tempfile
from pathlib import Path

import pytest

from helios.state import HeliosDB

# ── paths ────────────────────────────────────────────────────────
MIGRATIONS_DIR: Path = Path(__file__).resolve().parent.parent / "helios" / "migrations"

# NNN_*.sql pattern: three digits, underscore, then anything ending in .sql
_MIGRATION_RE = re.compile(r"^\d{3}_.*\.sql$")

# ── fixtures ─────────────────────────────────────────────────────

@pytest.fixture()
def fresh_db(tmp_path):
    """Provide a HeliosDB pointing at a brand-new temp database, cleaning up after."""
    db_file = tmp_path / "test_helios.db"
    db = HeliosDB(db_path=str(db_file))
    yield db
    db.close()


# ── tests ────────────────────────────────────────────────────────

class TestMigrationRegistry:
    """Ensure the SCHEMA_FILES list stays in sync with the migrations directory."""

    @staticmethod
    def _migration_sql_files() -> list[str]:
        """Return all filenames matching NNN_*.sql in the migrations directory."""
        if not MIGRATIONS_DIR.is_dir():
            pytest.skip(f"Migrations directory not found: {MIGRATIONS_DIR}")
        return sorted(
            f.name for f in MIGRATIONS_DIR.iterdir()
            if _MIGRATION_RE.match(f.name)
        )

    def test_migration_registry_complete(self):
        """Every NNN_*.sql file in the migrations directory must appear in SCHEMA_FILES.

        Files that are intentionally skipped should be added to
        ``_SKIPPED_MIGRATIONS`` below with a comment explaining why.
        """
        all_sql = self._migration_sql_files()
        registry_set = set(HeliosDB.SCHEMA_FILES)

        # ── Intentionally skipped migrations (must have a reason) ──
        # These files exist in migrations/ but are deliberately not in
        # SCHEMA_FILES because they alter data that earlier migrations
        # already create, or are bridge/compat scripts that were folded
        # into the base schema.  Add entries here with a short reason.
        _SKIPPED_MIGRATIONS: dict[str, str] = {
            "012_v6_rules_columns.sql": "column additions folded into 010_v6_rules",
            "013_v3_to_v5_bridge.sql": "legacy bridge script, not needed on fresh DB",
            "014_v6_missing_tables.sql": "destructive (drops/recreates focus); tables recreated safely in 026_v6_schema_gaps",
            "015_disable_broken_rules.sql": "data-level migration, runs once on upgrade",
            "016_phase1_indexes.sql": "index additions folded into base schema",
            "017_focus_retention.sql": "table folded into 026_v6_schema_gaps",
            # 018 and 019 are now in SCHEMA_FILES (applied after 026 for idempotent index creation)
            # They are NOT skipped — they're listed here only as documentation of their prior skip status
        }

        missing = []
        for fname in all_sql:
            if fname not in registry_set:
                if fname in _SKIPPED_MIGRATIONS:
                    continue  # explicitly documented skip
                missing.append(fname)

        assert missing == [], (
            f"The following migration files are not in SCHEMA_FILES and are not "
            f"documented as intentionally skipped. Either add them to SCHEMA_FILES "
            f"or to _SKIPPED_MIGRATIONS with a reason: {missing}"
        )

    def test_migration_files_exist(self):
        """Every entry in SCHEMA_FILES must correspond to an actual .sql file."""
        missing = []
        for fname in HeliosDB.SCHEMA_FILES:
            fpath = MIGRATIONS_DIR / fname
            if not fpath.is_file():
                missing.append(fname)

        assert missing == [], (
            f"The following SCHEMA_FILES entries do not exist in the migrations "
            f"directory: {missing}"
        )

    def test_migration_idempotent(self, fresh_db):
        """Running _ensure_schema twice on a fresh DB should succeed without errors."""
        # fresh_db already ran _ensure_schema once during __init__
        # Run it a second time — must not raise
        fresh_db._ensure_schema()

        # Verify the database is still functional by querying a known table
        rows = fresh_db._execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = {r["name"] for r in rows}
        # At minimum the base schema should create the 'context' table
        assert "context" in table_names, (
            f"Expected 'context' table after idempotent schema run; "
            f"got tables: {sorted(table_names)}"
        )