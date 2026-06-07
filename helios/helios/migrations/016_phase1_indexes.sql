-- 016_phase1_indexes.sql
-- Phase 1 Memory + Preference hardening — required indexes.
-- Prevents full-table scans on focus and metric_snapshots during inference.
-- ChatGPT review blocker 4 / Phase 1.5 prerequisite.

CREATE INDEX IF NOT EXISTS idx_focus_state_ts ON focus(state, ts);
CREATE INDEX IF NOT EXISTS idx_focus_ts ON focus(ts);
CREATE INDEX IF NOT EXISTS idx_metric_snapshots_metric_date ON metric_snapshots(metric, date_key);
CREATE INDEX IF NOT EXISTS idx_correlations_strength ON correlations(strength);
