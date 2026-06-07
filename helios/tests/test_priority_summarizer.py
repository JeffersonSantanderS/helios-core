"""Tests for PrioritySummarizer."""

import pytest
import sqlite3
from datetime import datetime, timezone, timedelta
from helios.priority.summarizer import PrioritySummarizer


class FakeDB:
    """Fake DB that stores rows in memory."""
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        c = self.conn
        c.execute("""CREATE TABLE priority_candidates (
            id INTEGER PRIMARY KEY,
            candidate_id TEXT NOT NULL UNIQUE,
            tick_id TEXT,
            created_at TEXT,
            source TEXT,
            candidate_type TEXT,
            title TEXT,
            category TEXT,
            severity TEXT,
            status TEXT
        )""")
        c.execute("""CREATE TABLE priority_scores (
            id INTEGER PRIMARY KEY,
            candidate_id TEXT,
            tick_id TEXT,
            final_score REAL,
            explanation TEXT,
            created_at TEXT,
            urgency REAL, importance REAL, relevance REAL,
            confidence REAL, context_fit REAL, actionability REAL,
            novelty REAL, safety REAL, disruption_cost REAL,
            staleness REAL, annoyance REAL, redundancy REAL
        )""")
        c.execute("""CREATE TABLE priority_decisions (
            id INTEGER PRIMARY KEY,
            candidate_id TEXT,
            tick_id TEXT,
            decision TEXT,
            route TEXT,
            reason TEXT,
            final_score REAL,
            mode TEXT,
            created_at TEXT
        )""")
        c.commit()

    def _conn(self):
        return self.conn

    def _seed(self, n_candidates=3, n_selected=1, n_suppressed=1, n_deferred=0):
        now = datetime.now(timezone.utc)
        for i in range(n_candidates):
            cid = f"cand_{i}"
            self.conn.execute(
                "INSERT INTO priority_candidates VALUES (?,?,?,?,?,?,?,?,?,?)",
                (i, cid, "tick_1", now.isoformat(), "rules_v2", "rule_alert",
                 f"Alert {i}", "home" if i == 0 else "system", "warning", "generated")
            )
            score = 0.9 - i * 0.2
            self.conn.execute(
                "INSERT INTO priority_scores (candidate_id, tick_id, final_score, explanation, created_at, "
                "urgency, importance, relevance, confidence, context_fit, actionability, novelty, safety, "
                "disruption_cost, staleness, annoyance, redundancy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, "tick_1", score, "top factors: urgency=0.80, importance=0.70, relevance=0.60 | score: high",
                 now.isoformat(), 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.9, 0.1, 0.1, 0.1, 0.1)
            )
            if i < n_selected:
                dec = "select_notify"
                route = "channel"
                reason = "above threshold"
            elif i < n_selected + n_suppressed:
                dec = "suppress_duplicate"
                route = "log"
                reason = "same alert in last hour"
            else:
                dec = "defer"
                route = "summary"
                reason = "quiet hours"
            self.conn.execute(
                "INSERT INTO priority_decisions (candidate_id, tick_id, decision, route, reason, final_score, mode, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (cid, "tick_1", dec, route, reason, score, "shadow", now.isoformat())
            )
        self.conn.commit()


@pytest.fixture
def summarizer():
    db = FakeDB()
    db._seed(n_candidates=4, n_selected=1, n_suppressed=2, n_deferred=1)
    return PrioritySummarizer(db)


class TestSummarizerGenerate:
    def test_totals(self, summarizer):
        s = summarizer.generate(hours=24)
        assert s["totals"]["generated"] == 4
        assert s["totals"]["scored"] == 4
        assert s["totals"]["selected"] == 1
        assert s["totals"]["suppressed"] == 2
        assert s["totals"]["deferred"] == 1

    def test_top_candidates_sorted(self, summarizer):
        s = summarizer.generate(hours=24)
        top = s["top_candidates"]
        assert len(top) == 4
        assert top[0]["score"] >= top[1]["score"]
        assert top[0]["title"] == "Alert 0"

    def test_score_stats(self, summarizer):
        s = summarizer.generate(hours=24)
        ss = s["score_stats"]
        assert ss["count"] == 4
        assert ss["max"] >= ss["min"]
        assert ss["avg"] > 0

    def test_category_counts(self, summarizer):
        s = summarizer.generate(hours=24)
        assert s["categories"]["home"] == 1
        assert s["categories"]["system"] == 3

    def test_suppressed_reasons(self, summarizer):
        s = summarizer.generate(hours=24)
        assert len(s["suppressed_reasons"]) == 2
        assert "same alert" in s["suppressed_reasons"][0]["reason"]

    def test_empty_window(self, summarizer):
        s = summarizer.generate(hours=0)
        assert s["totals"]["generated"] == 0
        assert s["top_candidates"] == []


class TestSummarizerWrite:
    def test_writes_files(self, summarizer, tmp_path):
        # Override paths
        from helios.priority import summarizer as s_mod
        s_mod.SUMMARY_DIR = tmp_path / "summaries"
        s_mod.SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
        s_mod.DATA_DIR = tmp_path
        summary = summarizer.generate(hours=24)
        out = summarizer.write(summary, tag="test")
        assert out.exists()
        latest = tmp_path / "latest_summary.json"
        assert latest.exists()


class TestDiscordEmbed:
    def test_embed_structure(self, summarizer):
        summary = summarizer.generate(hours=24)
        embed = summarizer.matrix_embed(summary)
        assert "Priority Engine Summary" in embed["title"]
        assert "Top Candidates" in embed["description"]
        assert embed["color"] == 0x3498db


class TestSummarizerQuery:
    def test_explain_cli(self, summarizer):
        rows = summarizer._query_all(
            "SELECT candidate_id, title FROM priority_candidates WHERE candidate_id = ?",
            ("cand_0",)
        )
        assert len(rows) == 1
        assert rows[0]["title"] == "Alert 0"
