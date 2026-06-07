-- Helios Migration 005: NL Queries Audit Log
-- Tracks natural language queries for the query bridge (SAN-118)
-- Stores every query for rate limiting, audit, and improvement

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- ============================================================================
-- nl_queries: Audit log for natural language queries via Discord DM
-- ============================================================================
CREATE TABLE IF NOT EXISTS nl_queries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query_text      TEXT    NOT NULL,                        -- Raw user query text
    parsed_intent   TEXT,                                    -- JSON: extracted module, metric, time range, comparison
    sql_query       TEXT,                                    -- Generated SQL query
    result_summary  TEXT,                                    -- LLM-formatted response sent back
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'done', 'failed', 'fallback')),
    tokens_used    INTEGER NOT NULL DEFAULT 0,              -- Token count for the LLM call
    error           TEXT,                                    -- Error message if failed
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Index for rate limiting queries (count per hour)
CREATE INDEX IF NOT EXISTS idx_nl_queries_ts ON nl_queries (ts);

-- Index for status-based filtering
CREATE INDEX IF NOT EXISTS idx_nl_queries_status ON nl_queries (status);

-- ============================================================================
-- Schema version tracking
-- ============================================================================
INSERT OR IGNORE INTO schema_version (version, description) VALUES (5, 'NL Queries — audit log for query bridge (SAN-118)');