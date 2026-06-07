-- Helios Migration 004: Correlations
-- Adds correlations and metrics_snapshot tables for the Cross-Module Correlation Engine (SAN-117)
-- Stores discovered correlations between module metrics with strength, direction, confidence
-- metric_snapshots enables daily aggregation of context-only metrics for time-series analysis

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- ============================================================================
-- metric_snapshots: Daily aggregated values of module metrics for time-series analysis
-- ============================================================================
-- This table stores daily snapshots of metrics that only live in the context table
-- (which is single-value per key). The correlator populates this during scans by
-- reading current values and backfilling from context history.
CREATE TABLE IF NOT EXISTS metric_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metric          TEXT    NOT NULL,                -- e.g., 'protein.grams_daily', 'sleep.hours'
    value           REAL    NOT NULL,                 -- The aggregated value for this day
    date_key        TEXT    NOT NULL,                -- YYYY-MM-DD
    source          TEXT    NOT NULL DEFAULT 'correlator',  -- 'correlator', 'module_tick'
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT uq_metric_date UNIQUE (metric, date_key)
);

CREATE INDEX IF NOT EXISTS idx_metric_snapshots_metric_date ON metric_snapshots (metric, date_key);
CREATE INDEX IF NOT EXISTS idx_metric_snapshots_ts ON metric_snapshots (ts);

-- ============================================================================
-- correlations: Discovered relationships between module metrics
-- ============================================================================
CREATE TABLE IF NOT EXISTS correlations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metric_a        TEXT    NOT NULL,                -- e.g., 'protein.grams_daily'
    metric_b        TEXT    NOT NULL,                -- e.g., 'sleep.hours'
    window_days     INTEGER NOT NULL DEFAULT 7,      -- Rolling window: 7, 14, or 28
    pearson_r       REAL    NOT NULL,                -- Pearson correlation coefficient
    p_value         REAL    NOT NULL,                -- Statistical significance
    strength        TEXT    NOT NULL CHECK (strength IN ('weak', 'moderate', 'strong')),
    direction       TEXT    NOT NULL CHECK (direction IN ('positive', 'negative')),
    n_observations  INTEGER NOT NULL,                -- Number of paired data points
    suggested_rule  TEXT,                             -- JSON: auto-suggested rule config (needs approval)
    approved        INTEGER NOT NULL DEFAULT 0,      -- 0=pending, 1=approved
    approved_by     TEXT,                             -- Who approved (NULL if pending)
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    -- Prevent duplicate correlation records for same pair+window
    CONSTRAINT uq_correlation_pair_window UNIQUE (metric_a, metric_b, window_days)
);

-- Indexes for correlation queries
CREATE INDEX IF NOT EXISTS idx_correlations_strength   ON correlations (strength, pearson_r DESC);
CREATE INDEX IF NOT EXISTS idx_correlations_metric_a   ON correlations (metric_a);
CREATE INDEX IF NOT EXISTS idx_correlations_metric_b   ON correlations (metric_b);
CREATE INDEX IF NOT EXISTS idx_correlations_approved   ON correlations (approved);
CREATE INDEX IF NOT EXISTS idx_correlations_ts         ON correlations (ts);

-- ============================================================================
-- correlation_observations: Raw paired observations used for correlation calc
-- ============================================================================
CREATE TABLE IF NOT EXISTS correlation_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metric_a        TEXT    NOT NULL,
    metric_b        TEXT    NOT NULL,
    value_a         REAL    NOT NULL,
    value_b         REAL    NOT NULL,
    date_key        TEXT    NOT NULL,                -- YYYY-MM-DD for dedup
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT uq_obs_pair_date UNIQUE (metric_a, metric_b, date_key)
);

CREATE INDEX IF NOT EXISTS idx_corr_obs_pair_date ON correlation_observations (metric_a, metric_b, date_key);

-- ============================================================================
-- Schema version tracking
-- ============================================================================
INSERT OR IGNORE INTO schema_version (version, description) VALUES (4, 'Cross-module correlation engine — metric_snapshots, correlations, and correlation_observations tables (SAN-117)');