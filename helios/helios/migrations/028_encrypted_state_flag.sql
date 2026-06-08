-- ===========================================================================
-- Helios v6 — Migration 028: encrypted_state flag for sensitive modules
-- ===========================================================================
-- Adds an encrypted_state column to module_health to track which modules
-- have their state files encrypted at rest.  This is a metadata flag only;
-- actual encryption is handled by helios.crypto + BaseMod._save_state_encrypted.

-- Add encrypted_state column (default 0 = plaintext)
ALTER TABLE module_health ADD COLUMN encrypted_state INTEGER NOT NULL DEFAULT 0;

-- ── Schema version ─────────────────────────────────────────────────────
INSERT OR IGNORE INTO schema_version (version, description)
VALUES (28, 'Add encrypted_state column to module_health for PII modules');