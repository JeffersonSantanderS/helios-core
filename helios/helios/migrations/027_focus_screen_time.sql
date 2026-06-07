-- ============================================================================
-- Helios v6 — Focus table migration: widen CHECK to include 'screen_time'
-- ============================================================================
-- This is a ONE-TIME migration. It must only run if schema_version 26
-- doesn't already exist (i.e., the focus table hasn't been migrated yet).
--
-- SQLite cannot ALTER a CHECK constraint, so we must recreate the table.
-- This is safe because:
--   1. On a fresh DB the focus table is empty (just created by 001).
--   2. On an existing DB all current values ('working','gaming','idle',
--      'meeting','break') still pass the new wider CHECK.
--   3. Data is preserved through the rename-swap.
-- ============================================================================

-- Step 1: Create new focus table with updated CHECK
CREATE TABLE IF NOT EXISTS focus_v2 (
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

-- Step 2: Migrate data from old focus table (if it has data)
-- Use INSERT OR IGNORE to handle the case where focus already has rows
-- that might conflict (shouldn't happen, but defensive)
INSERT OR IGNORE INTO focus_v2
    (id, ts, state, source, context, duration_secs, session_start, session_end, created_at)
SELECT id, ts, state, source, context, duration_secs, session_start, session_end, created_at
FROM focus;

-- Step 3: Drop old table and rename new one
DROP TABLE IF EXISTS focus;
ALTER TABLE focus_v2 RENAME TO focus;

-- Step 4: Recreate indexes on the renamed focus table
CREATE INDEX IF NOT EXISTS idx_focus_state_ts  ON focus (state, ts);
CREATE INDEX IF NOT EXISTS idx_focus_source_ts ON focus (source, ts);
CREATE INDEX IF NOT EXISTS idx_focus_session   ON focus (session_start, session_end) WHERE session_end IS NULL;

-- ── Schema version ─────────────────────────────────────────────────────
INSERT OR IGNORE INTO schema_version (version, description) VALUES (27, 'Focus table migration: widen CHECK constraint to include screen_time');