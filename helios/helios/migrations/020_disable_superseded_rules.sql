-- Helios Migration 020: Disable superseded legacy rules
--
-- These rules were superseded by v6 deterministic systems (dream engine,
-- briefing module, daily intelligence). They cause duplicate notifications,
-- raw template variable spam ({weather.temp_c}), and unnecessary rule
-- evaluation every tick.
--
-- Disabled rules:
--   dream_cycle_complete — autoDream has its own internal notification
--   morning_checkin     — briefing module owns morning briefings
--   evening_wrap        — same class of bug, references stale module keys

UPDATE rules
SET enabled = 0
WHERE slug IN (
    'dream_cycle_complete',
    'morning_checkin',
    'evening_wrap'
);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (20, 'Disable superseded legacy rules (dream_cycle_complete, morning_checkin, evening_wrap)');
