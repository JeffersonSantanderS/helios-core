-- =============================================================================
-- Phase 3.5 — Timeline Compression + Salience
-- =============================================================================
-- Compresses raw timeline_events + focus rows into operational sessions,
-- scores everything by salience, and extracts notable events.
--
-- Design order: raw events → grouped sessions → compressed windows →
--   salience scoring → notable-event extraction → (future) narrative layer
-- =============================================================================

-- Consolidated sessions: one row per contiguous same-state focus block
-- plus high-level events (sleep, mood, location, alerts) promoted to sessions
CREATE TABLE IF NOT EXISTS timeline_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_type    TEXT    NOT NULL,  -- focus_block, sleep_period, mood_log,
                                      --   location_trip, alert_cluster
    date_key        TEXT    NOT NULL,  -- YYYY-MM-DD (MDT boundary)
    session_start   TEXT    NOT NULL,  -- ISO-8601 UTC
    session_end     TEXT    NOT NULL,
    duration_secs   REAL    NOT NULL DEFAULT 0,
    dominant_state  TEXT,             -- focus state or event type
    event_count     INTEGER NOT NULL DEFAULT 0,
    source_events   TEXT,             -- JSON array of timeline_event IDs
    summary         TEXT    NOT NULL,  -- one-line human-readable
    metadata        TEXT,             -- JSON blob: state breakdown, avg importance
    confidence      REAL    NOT NULL DEFAULT 0.5,
    importance      REAL    NOT NULL DEFAULT 0.5,  -- salience score
    novelty         REAL    NOT NULL DEFAULT 0.3,  -- how unusual is this session
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Per-session metric breakdowns
CREATE TABLE IF NOT EXISTS session_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES timeline_sessions(id) ON DELETE CASCADE,
    metric_key      TEXT    NOT NULL,  -- e.g., focus.working.secs, mood.avg, alert.count
    metric_value    REAL    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Notable events: daily top-events extraction (deterministic, no LLM)
CREATE TABLE IF NOT EXISTS notable_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date_key        TEXT    NOT NULL,
    rank            INTEGER NOT NULL,  -- 1–N, 1 = most notable
    event_type      TEXT    NOT NULL,  -- top_anomaly, top_correlation, top_mood_shift,
                                      --   top_location_change, top_session, top_alert
    session_id      INTEGER REFERENCES timeline_sessions(id) ON DELETE SET NULL,
    timeline_event_id INTEGER REFERENCES timeline_events(id) ON DELETE SET NULL,
    summary         TEXT    NOT NULL,
    importance      REAL    NOT NULL,
    novelty         REAL    NOT NULL,
    confidence      REAL    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_sessions_date     ON timeline_sessions(date_key);
CREATE INDEX IF NOT EXISTS idx_sessions_type     ON timeline_sessions(session_type);
CREATE INDEX IF NOT EXISTS idx_sessions_start    ON timeline_sessions(session_start);
CREATE INDEX IF NOT EXISTS idx_sessions_imp      ON timeline_sessions(importance DESC);
CREATE INDEX IF NOT EXISTS idx_sm_session        ON session_metrics(session_id);
CREATE INDEX IF NOT EXISTS idx_notable_date      ON notable_events(date_key);
CREATE INDEX IF NOT EXISTS idx_notable_rank      ON notable_events(date_key, rank);

INSERT OR IGNORE INTO schema_version (version, applied_at, description)
VALUES (19, datetime('now'), 'Phase 3.5: timeline_sessions + session_metrics + notable_events for compression and salience');
