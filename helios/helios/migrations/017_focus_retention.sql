-- 017_focus_retention.sql
-- Phase 1.5 — DB Hygiene: retention, aggregates, indexes.
-- Focus table grows ~80K rows/day from 15s collectors.
-- This migration:
--   1. Creates focus_daily_summary for fast inferencing
--   2. Adds retention helper (no automatic deletion — triggered by engine)

CREATE TABLE IF NOT EXISTS focus_daily_summary (
    date_key    TEXT NOT NULL,
    state       TEXT NOT NULL,
    total_secs  REAL NOT NULL DEFAULT 0,
    session_count INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT,
    last_seen   TEXT,
    PRIMARY KEY (date_key, state)
) WITHOUT ROWID;

-- Pre-populate from existing focus data (can be slow on 4GB DB — one-time cost)
INSERT OR IGNORE INTO focus_daily_summary (date_key, state, total_secs, session_count, first_seen, last_seen)
SELECT
    substr(ts, 1, 10),
    state,
    COALESCE(SUM(duration_secs), 0),
    COUNT(*),
    MIN(ts),
    MAX(ts)
FROM focus
GROUP BY substr(ts, 1, 10), state;
