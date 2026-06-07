-- ============================================================================
-- Helios v6 — PR #2 Hardening: Missing tables + focus screen_time fix
-- ============================================================================
-- Creates tables that were being lazily created at runtime:
--   module_health, reminders, action_log, prediction_outcomes
-- Also fixes focus table CHECK to include 'screen_time' state.
--
-- For focus: SQLite can't ALTER CHECK constraints, so we recreate the table.
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

-- ── focus: Recreate to allow 'screen_time' state ───────────────────────
-- SQLite doesn't support ALTER TABLE DROP CONSTRAINT, so we must recreate.

-- 1. Create new focus table with updated CHECK
CREATE TABLE IF NOT EXISTS focus_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    state           TEXT    NOT NULL CHECK (state IN ('working', 'gaming', 'idle', 'meeting', 'break', 'screen_time')),
    source          TEXT    NOT NULL,
    context         TEXT    NOT NULL DEFAULT '{}',
    duration_secs   INTEGER,
    session_start   TEXT,
    session_end     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 2. Copy existing data (if focus table already exists)
INSERT OR IGNORE INTO focus_new
    (id, ts, state, source, context, duration_secs, session_start, session_end, created_at)
SELECT id, ts, state, source, context, duration_secs, session_start, session_end, created_at
FROM focus;

-- 3. Drop old table and rename new one
DROP TABLE IF EXISTS focus;
ALTER TABLE focus_new RENAME TO focus;

-- 4. Recreate indexes
CREATE INDEX IF NOT EXISTS idx_focus_state_ts  ON focus (state, ts);
CREATE INDEX IF NOT EXISTS idx_focus_source_ts ON focus (source, ts);
CREATE INDEX IF NOT EXISTS idx_focus_session   ON focus (session_start, session_end) WHERE session_end IS NULL;

-- ── Schema version ─────────────────────────────────────────────────────
INSERT OR IGNORE INTO schema_version (version, description) VALUES (14, 'PR #2 hardening — module_health, reminders, action_log, prediction_outcomes, focus screen_time');
