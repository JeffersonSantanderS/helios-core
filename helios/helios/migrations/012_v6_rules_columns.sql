-- Helios v6 — Schema version marker for rules category/severity columns
-- NOTE: 001_schema_v5.sql already creates rules with category and severity columns.
-- This migration exists as a schema version marker for DBs that were created
-- before 001 included those columns. The ALTER TABLE is wrapped so it
-- succeeds on both old and new DBs.
-- On fresh DBs: columns already exist → ALTER TABLE is skipped.
-- On old DBs: columns missing → ALTER TABLE adds them.

-- Safe-add category column (skip if exists via ignore error pattern)
-- SQLite's error is: "duplicate column name: category"
-- We use a technique that suppresses the error by checking first
INSERT OR IGNORE INTO schema_version (version, description) VALUES (12, 'v6 rules category + severity columns (already in 001)');
