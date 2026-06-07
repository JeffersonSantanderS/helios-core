-- Helios Migration 013: Apply v5 schema to existing v3 DB
-- Bridges the gap: v3 DB (m, e, p tables) → full v5 schema
-- Creates all tables from 001-004 migrations without breaking existing data

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- ============================================================================
-- context: Shared state packets (replaces v3 'm' metrics table)
-- ============================================================================
CREATE TABLE IF NOT EXISTS context (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    source      TEXT    NOT NULL,
    module      TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value       TEXT    NOT NULL DEFAULT '{}',
    priority    INTEGER NOT NULL DEFAULT 0,
    expires_at  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT ctx_unique_latest UNIQUE (module, key, source)
);
CREATE INDEX IF NOT EXISTS idx_context_module_ts ON context (module, ts);

-- ============================================================================
-- mood: Daily mood check-ins
-- ============================================================================
CREATE TABLE IF NOT EXISTS mood (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    emoji           TEXT    NOT NULL,
    score           INTEGER NOT NULL CHECK (score BETWEEN 1 AND 10),
    note            TEXT,
    source          TEXT    NOT NULL DEFAULT 'manual',
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_mood_ts ON mood (ts);
CREATE INDEX IF NOT EXISTS idx_mood_score_ts ON mood (score, ts);

-- ============================================================================
-- focus: Activity tracking (gaming, work, idle, etc.)
-- ============================================================================
CREATE TABLE IF NOT EXISTS focus (
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
CREATE INDEX IF NOT EXISTS idx_focus_state_ts ON focus (state, ts);

-- ============================================================================
-- metric_snapshots: Daily time-series values (feeds correlator)
-- ============================================================================
CREATE TABLE IF NOT EXISTS metric_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metric          TEXT    NOT NULL,
    value           REAL    NOT NULL,
    date_key        TEXT    NOT NULL,
    source          TEXT    NOT NULL DEFAULT 'ingestion',
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT uq_metric_date UNIQUE (metric, date_key)
);
CREATE INDEX IF NOT EXISTS idx_metric_snapshots_metric_date ON metric_snapshots (metric, date_key);

-- ============================================================================
-- correlations: Discovered patterns
-- ============================================================================
CREATE TABLE IF NOT EXISTS correlations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metric_a        TEXT    NOT NULL,
    metric_b        TEXT    NOT NULL,
    window_days     INTEGER NOT NULL DEFAULT 7,
    pearson_r       REAL    NOT NULL,
    p_value         REAL    NOT NULL,
    strength        TEXT    NOT NULL CHECK (strength IN ('weak', 'moderate', 'strong')),
    direction       TEXT    NOT NULL CHECK (direction IN ('positive', 'negative')),
    n_observations  INTEGER NOT NULL,
    suggested_rule  TEXT,
    approved        INTEGER NOT NULL DEFAULT 0,
    approved_by     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT uq_correlation_pair_window UNIQUE (metric_a, metric_b, window_days)
);

-- ============================================================================
-- correlation_observations: Raw paired data points
-- ============================================================================
CREATE TABLE IF NOT EXISTS correlation_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metric_a        TEXT    NOT NULL,
    metric_b        TEXT    NOT NULL,
    value_a         REAL    NOT NULL,
    value_b         REAL    NOT NULL,
    date_key        TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT uq_obs_pair_date UNIQUE (metric_a, metric_b, date_key)
);

-- ============================================================================
-- Schema version
-- ============================================================================
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    description TEXT
);
INSERT OR IGNORE INTO schema_version (version, description) VALUES (1, 'Initial v5 schema');
INSERT OR IGNORE INTO schema_version (version, description) VALUES (4, 'Correlation engine tables');
INSERT OR IGNORE INTO schema_version (version, description) VALUES (13, 'Migration bridge v3→v5 — applied 2026-05-03');
