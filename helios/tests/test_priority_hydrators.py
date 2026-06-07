"""Tests for Priority Engine hydrators — repeat/annoyance history queries."""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from helios.priority.hydrators import CandidateHydrator
from helios.priority.config import PriorityConfig
from helios.priority.models import Candidate


class FakeDB:
    """Minimal in-memory DB that supports _execute queries."""

    def __init__(self):
        import sqlite3
        self._sqlite = sqlite3.connect(":memory:")
        self._sqlite.execute("""
            CREATE TABLE priority_candidates (
                candidate_id TEXT PRIMARY KEY,
                fingerprint TEXT,
                source TEXT,
                candidate_type TEXT,
                title TEXT,
                severity TEXT,
                category TEXT,
                created_at TEXT
            )
        """)
        self._sqlite.execute("""
            CREATE TABLE priority_decisions (
                id INTEGER PRIMARY KEY,
                candidate_id TEXT,
                tick_id TEXT,
                decision TEXT,
                route TEXT,
                reason TEXT,
                final_score REAL,
                threshold_used REAL,
                execute_now INTEGER,
                mode TEXT,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
        """)
        self._sqlite.commit()

    def _execute(self, sql, params=()):
        return self._sqlite.execute(sql, params)

    def close(self):
        self._sqlite.close()


def _insert_decision(db, candidate_id, decision, reason, minutes_ago=0):
    db._execute(
        """INSERT INTO priority_decisions
           (candidate_id, tick_id, decision, route, reason, final_score, mode, ts)
           VALUES (?, 't1', ?, 'log', ?, 0.5, 'shadow',
                   datetime('now', '-{} minutes'))""".format(minutes_ago),
        (candidate_id, decision, reason),
    )
    db._sqlite.commit()


class TestPriorityHistory:
    def test_same_candidate_1h_counts_recent(self):
        db = FakeDB()
        db._execute(
            """INSERT INTO priority_candidates
               (candidate_id, fingerprint, source, candidate_type, title, severity, category, created_at)
               VALUES ('c1', 'fp_stable', 'test', 'test', 'T', 'info', 'system', datetime('now'))"""
        )
        db._sqlite.commit()
        _insert_decision(db, "c1", "select_log_only", "ok", minutes_ago=5)
        _insert_decision(db, "c1", "select_log_only", "ok", minutes_ago=10)
        _insert_decision(db, "c1", "select_log_only", "ok", minutes_ago=70)  # outside 1h

        cfg = PriorityConfig()
        hydrator = CandidateHydrator(db, cfg)
        cand = Candidate(candidate_id="c1", tick_id="t1", created_at="now",
                         source="test", candidate_type="test", title="T", severity="info", category="system",
                         fingerprint="fp_stable")

        hist = hydrator._get_priority_history(cand)
        # With fingerprint, same_fingerprint should count
        assert hist["same_fingerprint_1h"] == 2
        assert hist["same_fingerprint_24h"] == 3
        # candidate_id fallback not used when fingerprint present
        assert hist["same_candidate_1h"] == 0
        assert hist["last_decision"] == "select_log_only"
        db.close()

    def test_fingerprint_history_across_different_candidate_ids(self):
        """Same fingerprint with different candidate_ids should still count as same condition."""
        db = FakeDB()
        db._execute(
            """INSERT INTO priority_candidates (candidate_id, fingerprint, source, candidate_type, title, severity, category, created_at)
               VALUES ('c_old1', 'fp_shared', 'test', 'test', 'T', 'info', 'system', datetime('now', '-2 hours'))"""
        )
        db._execute(
            """INSERT INTO priority_candidates (candidate_id, fingerprint, source, candidate_type, title, severity, category, created_at)
               VALUES ('c_old2', 'fp_shared', 'test', 'test', 'T', 'info', 'system', datetime('now', '-30 minutes'))"""
        )
        db._execute(
            """INSERT INTO priority_decisions (candidate_id, tick_id, decision, route, reason, final_score, mode, ts)
               VALUES ('c_old1', 't0', 'select_log_only', 'log', 'ok', 0.5, 'shadow', datetime('now', '-2 hours'))"""
        )
        db._execute(
            """INSERT INTO priority_decisions (candidate_id, tick_id, decision, route, reason, final_score, mode, ts)
               VALUES ('c_old2', 't0', 'select_log_only', 'log', 'ok', 0.5, 'shadow', datetime('now', '-30 minutes'))"""
        )
        db._sqlite.commit()

        cfg = PriorityConfig()
        hydrator = CandidateHydrator(db, cfg)
        # New candidate, same fingerprint, new candidate_id
        cand = Candidate(candidate_id="c_new", tick_id="t1", created_at="now",
                         source="test", candidate_type="test", title="T", severity="info", category="system",
                         fingerprint="fp_shared")
        hist = hydrator._get_priority_history(cand)
        assert hist["same_fingerprint_1h"] == 1  # only the 30-min old one
        assert hist["same_fingerprint_24h"] == 2  # both
        db.close()

    def test_same_candidate_empty_when_no_history(self):
        db = FakeDB()
        cfg = PriorityConfig()
        hydrator = CandidateHydrator(db, cfg)
        cand = Candidate(candidate_id="c_new", tick_id="t1", created_at="now",
                         source="test", candidate_type="test", title="T", severity="info", category="system")

        hist = hydrator._get_priority_history(cand)
        assert hist["same_candidate_1h"] == 0
        assert hist["same_candidate_24h"] == 0
        assert hist["last_decision"] is None
        db.close()

    def test_same_title_1h_counts_reason_matches(self):
        db = FakeDB()
        # Insert decisions where reason contains the candidate title
        db._execute(
            """INSERT INTO priority_decisions
               (candidate_id, tick_id, decision, route, reason, final_score, mode, ts)
               VALUES ('cX', 't1', 'select_log_only', 'log',
                       'Score >= summary threshold | Temp too hot', 0.5, 'shadow',
                       datetime('now', '-5 minutes'))"""
        )
        db._execute(
            """INSERT INTO priority_decisions
               (candidate_id, tick_id, decision, route, reason, final_score, mode, ts)
               VALUES ('cY', 't1', 'select_log_only', 'log',
                       'Something else', 0.5, 'shadow',
                       datetime('now', '-5 minutes'))"""
        )
        db._sqlite.commit()

        cfg = PriorityConfig()
        hydrator = CandidateHydrator(db, cfg)
        cand = Candidate(candidate_id="c_new", tick_id="t1", created_at="now",
                         source="test", candidate_type="test",
                         title="Temp too hot", severity="warning", category="system")

        hist = hydrator._get_priority_history(cand)
        assert hist["same_title_1h"] == 1
        db.close()

    def test_no_db_returns_all_zeros(self):
        cfg = PriorityConfig()
        hydrator = CandidateHydrator(None, cfg)
        cand = Candidate(candidate_id="c1", tick_id="t1", created_at="now",
                         source="test", candidate_type="test", title="T", severity="info", category="system",
                         fingerprint="fp_test")
        hist = hydrator._get_priority_history(cand)
        assert hist["same_fingerprint_1h"] == 0
        assert hist["same_fingerprint_24h"] == 0
        assert hist["same_candidate_1h"] == 0
        assert hist["same_title_1h"] == 0
        assert hist["same_category_1h"] == 0
        assert hist["recent_suppressions"] == 0
        assert hist["recent_selections"] == 0
        assert hist["last_decision"] is None
