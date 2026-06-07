-- Helios Migration 003: Briefing Log
-- Adds briefing_log table for the Briefing module (SAN-116)
-- Tracks sent briefings to prevent duplicate daily sends

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- ============================================================================
-- briefing_log: Dedup and audit trail for daily briefings
-- ============================================================================
CREATE TABLE IF NOT EXISTS briefing_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    briefing_type   TEXT    NOT NULL CHECK (briefing_type IN ('morning', 'evening')),
    sent_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    date_key        TEXT    NOT NULL,                        -- YYYY-MM-DD — prevents duplicate per day
    content_hash    TEXT,                                    -- Hash of content for dedup
    discord_msg_id  TEXT,                                    -- Discord message ID after send
    status          TEXT    NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'queued', 'failed')),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT uq_briefing_type_date UNIQUE (briefing_type, date_key)
);

-- Indexes for briefing queries
CREATE INDEX IF NOT EXISTS idx_briefing_log_date ON briefing_log (date_key);

-- ============================================================================
-- Schema version tracking
-- ============================================================================
INSERT OR IGNORE INTO schema_version (version, description) VALUES (3, 'Briefing module — briefing_log table for dedup (SAN-116)');