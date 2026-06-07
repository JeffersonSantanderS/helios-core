-- Helios Migration 006: Subscriptions & Bill Tracking (SAN-121)
-- Stores subscription data extracted from email receipt scanning
-- Tracks service name, amount, billing cycle, renewal dates, and alert status

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- ============================================================================
-- subscriptions: Tracked subscriptions and bills
-- ============================================================================
CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    service         TEXT    NOT NULL,                        -- Service name (e.g., 'Netflix', 'Spotify')
    provider        TEXT    NOT NULL DEFAULT 'unknown',      -- Detection provider ('Stripe', 'PayPal', etc.)
    amount          REAL    NOT NULL DEFAULT 0.0,            -- Cost per billing cycle
    currency        TEXT    NOT NULL DEFAULT 'CAD',          -- Currency code
    cycle           TEXT    NOT NULL DEFAULT 'monthly'
                    CHECK (cycle IN ('monthly', 'yearly', 'weekly', 'one-time', 'unknown')),
    last_payment    TEXT,                                    -- Date of last detected payment (YYYY-MM-DD)
    next_renewal    TEXT,                                    -- Estimated next renewal date (YYYY-MM-DD)
    source_email    TEXT,                                    -- Sender email that triggered detection
    source_msgid    TEXT,                                    -- Email Message-ID for deduplication
    confidence      REAL    NOT NULL DEFAULT 0.5,            -- Detection confidence (0.0-1.0)
    is_active       INTEGER NOT NULL DEFAULT 1,              -- 1=active subscription, 0=cancelled/expired
    alert_sent      INTEGER NOT NULL DEFAULT 0,              -- 1=renewal alert already sent for next_renewal
    notes           TEXT,                                    -- Manual notes or details
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT uq_subscription_service_cycle UNIQUE (service, cycle)
);

-- Index for renewal alert queries
CREATE INDEX IF NOT EXISTS idx_subscriptions_next_renewal ON subscriptions (next_renewal, is_active, alert_sent);

-- Index for active subscriptions
CREATE INDEX IF NOT EXISTS idx_subscriptions_active ON subscriptions (is_active, cycle);

-- Index for service lookups
CREATE INDEX IF NOT EXISTS idx_subscriptions_service ON subscriptions (service);

-- ============================================================================
-- email_scan_log: Track which emails have been processed (dedup)
-- ============================================================================
CREATE TABLE IF NOT EXISTS email_scan_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT    NOT NULL UNIQUE,                  -- Email Message-ID header
    sender          TEXT,                                    -- From address
    subject         TEXT,                                    -- Email subject
    scan_date       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    result          TEXT    NOT NULL DEFAULT 'skipped',     -- 'detected', 'skipped', 'error'
    subscription_id INTEGER,                                 -- FK to subscriptions.id if detected
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT fk_subscription FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)
);

-- Index for dedup queries
CREATE INDEX IF NOT EXISTS idx_email_scan_msgid ON email_scan_log (message_id);

-- ============================================================================
-- Schema version tracking
-- ============================================================================
INSERT OR IGNORE INTO schema_version (version, description) VALUES (6, 'Subscriptions & bill tracking (SAN-121)');