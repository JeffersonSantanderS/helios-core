-- Migration 021: Priority Engine tables
-- Stores candidates, scores, decisions, and feedback for the Helios Priority Engine.

CREATE TABLE IF NOT EXISTS priority_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL UNIQUE,
    tick_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    source TEXT NOT NULL,
    candidate_type TEXT NOT NULL,
    title TEXT,
    message TEXT,
    severity TEXT DEFAULT 'info',
    category TEXT DEFAULT 'system',
    priority_hint INTEGER DEFAULT 1,
    module TEXT,
    rule_slug TEXT,
    action_name TEXT,
    action_config_json TEXT,
    raw_payload_json TEXT,
    hydrated_json TEXT,
    tags_json TEXT,
    status TEXT DEFAULT 'generated'
);

CREATE INDEX IF NOT EXISTS idx_priority_candidates_tick
    ON priority_candidates(tick_id);

CREATE INDEX IF NOT EXISTS idx_priority_candidates_type
    ON priority_candidates(candidate_type);

CREATE INDEX IF NOT EXISTS idx_priority_candidates_rule
    ON priority_candidates(rule_slug);

CREATE INDEX IF NOT EXISTS idx_priority_candidates_created
    ON priority_candidates(created_at DESC);

CREATE TABLE IF NOT EXISTS priority_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    tick_id TEXT NOT NULL,
    urgency REAL DEFAULT 0,
    importance REAL DEFAULT 0,
    relevance REAL DEFAULT 0,
    confidence REAL DEFAULT 0,
    context_fit REAL DEFAULT 0,
    actionability REAL DEFAULT 0,
    novelty REAL DEFAULT 0,
    safety REAL DEFAULT 0,
    disruption_cost REAL DEFAULT 0,
    staleness REAL DEFAULT 0,
    annoyance REAL DEFAULT 0,
    redundancy REAL DEFAULT 0,
    final_score REAL NOT NULL,
    explanation TEXT,
    factors_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(candidate_id) REFERENCES priority_candidates(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_priority_scores_tick
    ON priority_scores(tick_id);

CREATE INDEX IF NOT EXISTS idx_priority_scores_score
    ON priority_scores(final_score DESC);

CREATE TABLE IF NOT EXISTS priority_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    tick_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    route TEXT,
    reason TEXT,
    final_score REAL,
    threshold_used REAL,
    execute_now INTEGER DEFAULT 0,
    mode TEXT DEFAULT 'shadow',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(candidate_id) REFERENCES priority_candidates(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_priority_decisions_tick
    ON priority_decisions(tick_id);

CREATE INDEX IF NOT EXISTS idx_priority_decisions_decision
    ON priority_decisions(decision);

CREATE TABLE IF NOT EXISTS priority_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT,
    feedback_type TEXT NOT NULL,
    value TEXT,
    source TEXT DEFAULT 'user',
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_priority_feedback_candidate
    ON priority_feedback(candidate_id);

INSERT OR IGNORE INTO schema_version (version, description) VALUES (21, 'Priority Engine tables: candidates, scores, decisions, feedback');
