-- Helios Migration 007: Goal Tracking with Milestones (SAN-122)
-- Stores goal definitions, progress tracking, and milestone notifications

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- ============================================================================
-- goals: Long-term targets with metrics, deadlines, and progress
-- ============================================================================
CREATE TABLE IF NOT EXISTS goals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT    NOT NULL UNIQUE,                   -- URL-friendly identifier (e.g. 'run-500-miles')
    name            TEXT    NOT NULL,                           -- Human-readable name (e.g. 'Run 500 miles by December')
    description     TEXT,                                       -- Optional longer description
    metric          TEXT    NOT NULL,                           -- What we're measuring (e.g. 'miles', 'books', 'dollars')
    target_value    REAL    NOT NULL,                           -- Goal target (e.g. 500.0)
    current_value   REAL    NOT NULL DEFAULT 0.0,              -- Current progress towards target
    unit            TEXT    NOT NULL DEFAULT '',                -- Display unit (e.g. 'mi', 'books', '$')
    deadline        TEXT,                                       -- Target deadline (YYYY-MM-DD)
    source          TEXT    NOT NULL DEFAULT 'manual',         -- 'manual', 'finance', 'health', 'activity'
    source_key      TEXT,                                       -- Context key to auto-pull from (e.g. 'protein.grams_daily')
    is_active       INTEGER NOT NULL DEFAULT 1,                -- 1=active, 0=completed/cancelled
    completed_at    TEXT,                                       -- When goal was completed (YYYY-MM-DD)
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT uq_goal_slug UNIQUE (slug)
);

CREATE INDEX IF NOT EXISTS idx_goals_active ON goals (is_active, deadline);
CREATE INDEX IF NOT EXISTS idx_goals_source ON goals (source, source_key);

-- ============================================================================
-- goal_progress: Time-series progress entries for each goal
-- ============================================================================
CREATE TABLE IF NOT EXISTS goal_progress (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id         INTEGER NOT NULL,                           -- FK to goals.id
    value           REAL    NOT NULL,                           -- Progress value at this point
    delta           REAL    NOT NULL DEFAULT 0.0,              -- Change from previous entry
    note            TEXT,                                       -- Optional note about this progress entry
    source          TEXT    NOT NULL DEFAULT 'manual',         -- 'manual', 'tick', 'discord', 'import'
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT fk_goal_progress_goal FOREIGN KEY (goal_id) REFERENCES goals (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_goal_progress_goal_ts ON goal_progress (goal_id, ts);
CREATE INDEX IF NOT EXISTS idx_goal_progress_ts ON goal_progress (ts);

-- ============================================================================
-- goal_milestones: Track which milestone notifications have been sent
-- ============================================================================
CREATE TABLE IF NOT EXISTS goal_milestones (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id         INTEGER NOT NULL,                           -- FK to goals.id
    milestone_pct   REAL    NOT NULL,                           -- Percentage milestone (25, 50, 75, 100)
    alerted_at      TEXT,                                       -- When the milestone alert was sent
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT uq_goal_milestone UNIQUE (goal_id, milestone_pct),
    CONSTRAINT fk_goal_milestone_goal FOREIGN KEY (goal_id) REFERENCES goals (id) ON DELETE CASCADE
);

-- ============================================================================
-- Schema version tracking
-- ============================================================================
INSERT OR IGNORE INTO schema_version (version, description) VALUES (7, 'Goal tracking with milestones (SAN-122)');