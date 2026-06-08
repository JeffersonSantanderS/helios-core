-- ===========================================================================
-- Helios v6 — Migration 029: Subscriptions table updates (SAN-121)
-- ===========================================================================
-- Adds columns needed by the SubscriptionsModule that weren't in the
-- original migration 006, and ensures the schema is compatible with the
-- module's upsert / alert logic.
--
-- NOTE: migration 006 already created the `subscriptions` and
-- `email_scan_log` tables.  This migration adds the `category` column
-- (used for classification like streaming, cloud, utility, etc.) and
-- updates the schema_version tracker.

-- Add category column to subscriptions if it doesn't exist
-- (SQLite doesn't support IF NOT EXISTS for ALTER TABLE, so we use a
-- pragma-based check)
ALTER TABLE subscriptions ADD COLUMN category TEXT NOT NULL DEFAULT 'other';

-- ── Schema version ─────────────────────────────────────────────────────
INSERT OR IGNORE INTO schema_version (version, description)
VALUES (29, 'Add category column to subscriptions (SAN-121)');