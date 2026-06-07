-- ============================================================================
-- Helios v6 — Schema gap fix: add missing tables (NO focus migration)
-- ============================================================================
-- This migration is SAFE and IDEMPOTENT:
--   • All tables use CREATE TABLE IF NOT EXISTS
--   • Focus migration is split into 027_focus_screen_time.sql
-- ============================================================================

-- ── module_health: circuit-breaker health tracking ─────────────────────
CREATE TABLE IF NOT EXISTS module_health (
    module   TEXT NOT NULL,
    status   TEXT NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    ts       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_module_health_module_ts ON module_health (module, ts);

-- ── reminders: scheduled reminders from action_engine + other modules ──
CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT    NOT NULL,
    priority   TEXT    NOT NULL DEFAULT 'medium',
    remind_at  TEXT,
    completed  INTEGER NOT NULL DEFAULT 0,
    source     TEXT    NOT NULL DEFAULT 'action_engine',
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_reminders_remind_at  ON reminders (remind_at, completed);
CREATE INDEX IF NOT EXISTS idx_reminders_source      ON reminders (source);

-- ── action_log: audit trail for action_engine.execute() ────────────────
CREATE TABLE IF NOT EXISTS action_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    action  TEXT    NOT NULL,
    params  TEXT    NOT NULL DEFAULT '{}',
    result  TEXT    NOT NULL DEFAULT '{}',
    success INTEGER NOT NULL DEFAULT 1,
    source  TEXT    NOT NULL DEFAULT 'action_engine',
    ts      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_action_log_ts     ON action_log (ts);
CREATE INDEX IF NOT EXISTS idx_action_log_action  ON action_log (action);

-- ── prediction_outcomes: prediction accuracy tracking ──────────────────
CREATE TABLE IF NOT EXISTS prediction_outcomes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_ts   TEXT    NOT NULL,
    eval_ts         TEXT    NOT NULL,
    metric          TEXT    NOT NULL,
    days_ahead      INTEGER NOT NULL,
    predicted_value REAL    NOT NULL,
    low_bound       REAL,
    actual_value    REAL,
    error           REAL,
    abs_pct_error   REAL,
    within_bounds   INTEGER,
    trend_slope     REAL,
    r_squared       REAL,
    days_data       INTEGER,
    resolved        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_prediction_outcomes_metric
    ON prediction_outcomes (metric, prediction_ts);

-- ── focus_daily_summary: aggregate focus data for fast inferencing ─────
CREATE TABLE IF NOT EXISTS focus_daily_summary (
    date_key      TEXT NOT NULL,
    state         TEXT NOT NULL,
    total_secs    REAL NOT NULL DEFAULT 0,
    session_count INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT,
    last_seen     TEXT,
    PRIMARY KEY (date_key, state)
) WITHOUT ROWID;

-- ── timeline_events: one row per significant occurrence across modules ─
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

CREATE TABLE IF NOT EXISTS event_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_event_id INTEGER NOT NULL REFERENCES timeline_events(id) ON DELETE CASCADE,
    target_event_id INTEGER NOT NULL REFERENCES timeline_events(id) ON DELETE CASCADE,
    link_type       TEXT    NOT NULL,
    confidence      REAL    NOT NULL DEFAULT 0.5,
    evidence        TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_timeline_ts      ON timeline_events(ts);
CREATE INDEX IF NOT EXISTS idx_timeline_type    ON timeline_events(event_type);
CREATE INDEX IF NOT EXISTS idx_timeline_date    ON timeline_events(date_key);
CREATE INDEX IF NOT EXISTS idx_timeline_import  ON timeline_events(importance DESC);
CREATE INDEX IF NOT EXISTS idx_timeline_source  ON timeline_events(source_module);

CREATE INDEX IF NOT EXISTS idx_el_source    ON event_links(source_event_id);
CREATE INDEX IF NOT EXISTS idx_el_target    ON event_links(target_event_id);
CREATE INDEX IF NOT EXISTS idx_el_type_conf ON event_links(link_type, confidence DESC);

-- ── timeline_sessions: contiguous sessions compressed from raw events ──
CREATE TABLE IF NOT EXISTS timeline_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_type    TEXT    NOT NULL,
    date_key        TEXT    NOT NULL,
    session_start   TEXT    NOT NULL,
    session_end     TEXT    NOT NULL,
    duration_secs   REAL    NOT NULL DEFAULT 0,
    dominant_state  TEXT,
    event_count     INTEGER NOT NULL DEFAULT 0,
    source_events   TEXT,
    summary         TEXT    NOT NULL,
    metadata        TEXT,
    confidence      REAL    NOT NULL DEFAULT 0.5,
    importance      REAL    NOT NULL DEFAULT 0.5,
    novelty         REAL    NOT NULL DEFAULT 0.3,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS session_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES timeline_sessions(id) ON DELETE CASCADE,
    metric_key      TEXT    NOT NULL,
    metric_value    REAL    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notable_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date_key        TEXT    NOT NULL,
    rank            INTEGER NOT NULL,
    event_type      TEXT    NOT NULL,
    session_id      INTEGER REFERENCES timeline_sessions(id) ON DELETE SET NULL,
    timeline_event_id INTEGER REFERENCES timeline_events(id) ON DELETE SET NULL,
    summary         TEXT    NOT NULL,
    importance      REAL    NOT NULL,
    novelty         REAL    NOT NULL,
    confidence      REAL    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_date     ON timeline_sessions(date_key);
CREATE INDEX IF NOT EXISTS idx_sessions_type     ON timeline_sessions(session_type);
CREATE INDEX IF NOT EXISTS idx_sessions_start    ON timeline_sessions(session_start);
CREATE INDEX IF NOT EXISTS idx_sessions_imp      ON timeline_sessions(importance DESC);
CREATE INDEX IF NOT EXISTS idx_sm_session        ON session_metrics(session_id);
CREATE INDEX IF NOT EXISTS idx_notable_date      ON notable_events(date_key);
CREATE INDEX IF NOT EXISTS idx_notable_rank      ON notable_events(date_key, rank);

-- ── Schema version ─────────────────────────────────────────────────────
INSERT OR IGNORE INTO schema_version (version, description) VALUES (26, 'Schema gap fix: module_health, reminders, action_log, prediction_outcomes, focus_daily_summary, timeline_events, event_links, timeline_sessions, session_metrics, notable_events');