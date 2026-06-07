-- Helios Migration 002: Calendar Events
-- Adds calendar_events table for the Calendar module (SAN-114)
-- Stores iCloud-synced calendar events with busy/free and all-day awareness

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- ============================================================================
-- calendar_events: iCloud-synced calendar events
-- ============================================================================
CREATE TABLE IF NOT EXISTS calendar_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    icloud_id       TEXT    UNIQUE,                        -- iCloud Calendar event ID for sync
    title           TEXT    NOT NULL,                      -- Event title / summary
    location        TEXT,                                   -- Event location (nullable)
    start_time      TEXT    NOT NULL,                      -- ISO8601 start timestamp
    end_time        TEXT    NOT NULL,                      -- ISO8601 end timestamp
    is_all_day      INTEGER NOT NULL DEFAULT 0,            -- 1=all-day event, 0=timed
    busy_free       TEXT    NOT NULL DEFAULT 'busy',       -- 'busy' or 'free'
    source          TEXT    NOT NULL DEFAULT 'pyicloud',    -- 'pyicloud', 'manual'
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Indexes for calendar event queries
CREATE INDEX IF NOT EXISTS idx_cal_events_start   ON calendar_events (start_time);
CREATE INDEX IF NOT EXISTS idx_cal_events_icloud   ON calendar_events (icloud_id) WHERE icloud_id IS NOT NULL;

-- ============================================================================
-- Schema version tracking
-- ============================================================================
INSERT OR IGNORE INTO schema_version (version, description) VALUES (2, 'Calendar module — calendar_events table and indexes (SAN-114)');