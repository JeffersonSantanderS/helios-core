-- Migration 022: Add stable fingerprint to priority_candidates for repeat detection
-- A fingerprint is a deterministic hash based on the semantic content of a candidate
-- so that the same condition across ticks produces the same fingerprint.

ALTER TABLE priority_candidates ADD COLUMN fingerprint TEXT;
CREATE INDEX IF NOT EXISTS idx_priority_candidates_fingerprint
    ON priority_candidates(fingerprint);

INSERT OR IGNORE INTO schema_version (version, description) VALUES (22, 'Add fingerprint column to priority_candidates for repeat detection');
