"""Helios v6 — Test Rules Engine v2 (PR 3 hardening).

Covers:
  - Cooldown: rule fires once, suppressed until cooldown expires
  - Syntax normalization: AND, OR, true, false
  - Missing context keys don't crash evaluator
  - Boolean values from module outputs evaluate correctly
"""

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helios.rules_v2 import RulesEngine


class FreshDB:
    """Minimal DB wrapper with just the rules table — no seed data."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local_conn = sqlite3.connect(db_path)
        self._local_conn.row_factory = sqlite3.Row
        self._local_conn.execute("""
            CREATE TABLE IF NOT EXISTS rules (
                slug TEXT PRIMARY KEY,
                trigger_type TEXT DEFAULT 'threshold',
                trigger_config TEXT DEFAULT '{}',
                condition TEXT,
                action_type TEXT DEFAULT 'push',
                action_config TEXT DEFAULT '{}',
                priority INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                created_by TEXT DEFAULT 'test',
                approved_by TEXT DEFAULT 'test',
                description TEXT DEFAULT '',
                cooldown_secs INTEGER DEFAULT 0,
                category TEXT DEFAULT '',
                severity TEXT DEFAULT 'info',
                last_triggered TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self._local_conn.commit()

    def get_rules(self, enabled=True):
        sql = "SELECT * FROM rules WHERE enabled=1" if enabled else "SELECT * FROM rules"
        rows = self._local_conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def _execute(self, sql, params=()):
        return self._local_conn.execute(sql, params)

    def _conn(self):
        return self._local_conn

    def close(self):
        self._local_conn.close()


@pytest.fixture
def db():
    """Minimal DB with only a rules table — no seed rules."""
    p = Path(tempfile.mkdtemp()) / "clean.db"
    fdb = FreshDB(str(p))
    yield fdb
    fdb.close()
    p.unlink(missing_ok=True)


@pytest.fixture
def engine(db):
    return RulesEngine(db)


# === Cooldown tests (Priority 5) =========================================

def test_cooldown_fires_once(engine, db):
    """Rule with cooldown fires once, then is suppressed."""
    db._execute("""
        INSERT INTO rules (slug, condition, cooldown_secs)
        VALUES ('test_cooldown', 'weather.temp_c > 0', 3600)
    """)
    db._conn().commit()

    context = {"weather": {"temp_c": 10}}
    hits = engine.evaluate(context)
    assert len(hits) == 1
    assert hits[0]["slug"] == "test_cooldown"

    hits2 = engine.evaluate(context)
    assert len(hits2) == 0


def test_no_cooldown_fires_every_tick(engine, db):
    """Rule with cooldown_secs=0 fires every tick."""
    db._execute("""
        INSERT INTO rules (slug, condition, cooldown_secs)
        VALUES ('test_no_cooldown', 'weather.temp_c > 0', 0)
    """)
    db._conn().commit()

    context = {"weather": {"temp_c": 10}}
    hits1 = engine.evaluate(context)
    hits2 = engine.evaluate(context)
    assert len(hits1) == 1
    assert len(hits2) == 1


def test_cooldown_expires(engine, db):
    """Rule fires again after cooldown expires."""
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db._execute(
        "INSERT INTO rules (slug, condition, cooldown_secs, last_triggered) VALUES (?, ?, ?, ?)",
        ("test_expired", "weather.temp_c > 0", 3600, past),
    )
    db._conn().commit()

    context = {"weather": {"temp_c": 10}}
    hits = engine.evaluate(context)
    assert len(hits) == 1
    assert hits[0]["slug"] == "test_expired"


# === Syntax normalization tests (Priority 6) ==============================

def test_normalize_AND(engine, db):
    """`AND` keyword is normalized to `and`."""
    db._execute("""
        INSERT INTO rules (slug, condition)
        VALUES ('test_and', 'weather.temp_c > 5 AND weather.humidity < 80')
    """)
    db._conn().commit()

    context = {"weather": {"temp_c": 10, "humidity": 50}}
    hits = engine.evaluate(context)
    assert len(hits) == 1

    context2 = {"weather": {"temp_c": 10, "humidity": 90}}
    hits2 = engine.evaluate(context2)
    assert len(hits2) == 0


def test_normalize_OR(engine, db):
    """`OR` keyword is normalized to `or`."""
    db._execute("""
        INSERT INTO rules (slug, condition)
        VALUES ('test_or', 'weather.temp_c < 0 OR weather.temp_c > 30')
    """)
    db._conn().commit()

    context = {"weather": {"temp_c": -5}}
    hits = engine.evaluate(context)
    assert len(hits) == 1

    context2 = {"weather": {"temp_c": 20}}
    hits2 = engine.evaluate(context2)
    assert len(hits2) == 0


def test_normalize_true(engine, db):
    """`true` condition evaluates to True."""
    db._execute("""
        INSERT INTO rules (slug, condition, cooldown_secs)
        VALUES ('test_true', 'true', 3600)
    """)
    db._conn().commit()

    hits = engine.evaluate({})
    assert len(hits) == 1
    assert hits[0]["slug"] == "test_true"


def test_normalize_false(engine, db):
    """`false` condition evaluates to False (rule never fires)."""
    db._execute("""
        INSERT INTO rules (slug, condition)
        VALUES ('test_false', 'false')
    """)
    db._conn().commit()

    hits = engine.evaluate({"weather": {"temp_c": 100}})
    assert len(hits) == 0


# === Missing context keys ================================================

def test_missing_module_evaluates_false(engine, db):
    """Rule referencing nonexistent module evaluates to False, no crash."""
    db._execute("""
        INSERT INTO rules (slug, condition)
        VALUES ('test_missing_mod', 'nonexistent.foo > 0')
    """)
    db._conn().commit()

    hits = engine.evaluate({"weather": {"temp_c": 10}})
    assert len(hits) == 0


def test_missing_key_evaluates_false(engine, db):
    """Rule referencing nonexistent key in valid module evaluates False."""
    db._execute("""
        INSERT INTO rules (slug, condition)
        VALUES ('test_missing_key', 'weather.nonexistent_key > 50')
    """)
    db._conn().commit()

    hits = engine.evaluate({"weather": {"temp_c": 10}})
    assert len(hits) == 0


# === Boolean values =======================================================

def test_boolean_true(engine, db):
    """Boolean True from module output evaluates correctly."""
    db._execute("""
        INSERT INTO rules (slug, condition)
        VALUES ('test_bool_true', 'gaming.is_gaming == True')
    """)
    db._conn().commit()

    context = {"gaming": {"is_gaming": True}}
    hits = engine.evaluate(context)
    assert len(hits) == 1


def test_boolean_false(engine, db):
    """Boolean False from module output evaluates correctly."""
    db._execute("""
        INSERT INTO rules (slug, condition)
        VALUES ('test_bool_false', 'gaming.is_gaming == True')
    """)
    db._conn().commit()

    context = {"gaming": {"is_gaming": False}}
    hits = engine.evaluate(context)
    assert len(hits) == 0


def test_module_value_is_not_dict(engine, db):
    """Module returning a non-dict value doesn't crash the evaluator."""
    db._execute("""
        INSERT INTO rules (slug, condition)
        VALUES ('test_not_dict', 'location.city == \"Anytown\"')
    """)
    db._conn().commit()

    context = {"location": "Anytown"}
    hits = engine.evaluate(context)
    assert len(hits) == 0
