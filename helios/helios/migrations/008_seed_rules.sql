-- ============================================================================
-- Helios v5 — Seed Rules (Migration 005)
-- ============================================================================
-- These rules get the engine doing useful things immediately.
-- All rules are script_engine created (already approved, no approval needed).
-- ============================================================================

-- 1. Morning weather check — DM if cold outside
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, approved_by, description, cooldown_secs)
VALUES (
    'cold_weather_alert',
    'threshold',
    '{}',
    'weather.temp_c < 0',
    'discord_push',
    '{"action": "push", "message": "❄️ It''s below freezing today ({temp_c}°C). Bundle up!", "priority": 2}',
    2, 1, 'script_engine', 'helios',
    'Alert when temperature drops below freezing — DM user',
    21600
);

-- 2. Hot weather warning — channel post
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, approved_by, description, cooldown_secs)
VALUES (
    'hot_weather_alert',
    'threshold',
    '{}',
    'weather.temp_c > 28',
    'discord_push',
    '{"action": "push", "message": "🌡️ Hot day ahead: {temp_c}°C, feels like {feels_like_c}°C. Stay hydrated!", "priority": 1}',
    1, 1, 'script_engine', 'helios',
    'Alert when temperature exceeds 28°C',
    21600
);

-- 3. Protein check — DM reminder if behind on daily target
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, approved_by, description, cooldown_secs)
VALUES (
    'protein_behind_reminder',
    'threshold',
    '{}',
    'protein.pct < 50',
    'discord_push',
    '{"action": "push", "message": "🥩 Protein check: only {pct}% of your {target}g target so far ({today}g today). Time to eat!", "priority": 1}',
    1, 1, 'script_engine', 'helios',
    'DM when protein intake is below 50% of daily target',
    7200
);

-- 4. Protein goal met — congrats DM
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, approved_by, description, cooldown_secs)
VALUES (
    'protein_goal_met',
    'threshold',
    '{}',
    'protein.pct >= 100',
    'discord_push',
    '{"action": "push", "message": "💪 Protein goal met! {today}g / {target}g ({pct}%). Nice work!", "priority": 2}',
    2, 1, 'script_engine', 'helios',
    'Congratulate when protein goal is reached',
    28800
);

-- 5. Gaming session detected — log to channel (awareness)
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, approved_by, description, cooldown_secs)
VALUES (
    'gaming_session_active',
    'threshold',
    '{}',
    'gaming.count > 0',
    'notify',
    '{"action": "notify", "message": "🎮 Gaming active: {active_games}", "priority": 0}',
    0, 1, 'script_engine', 'helios',
    'Log when gaming is detected (informational only, no push)',
    0
);

-- 6. Busy calendar day — morning heads-up
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, approved_by, description, cooldown_secs)
VALUES (
    'busy_day_alert',
    'threshold',
    '{}',
    'calendar.today_event_count > 3',
    'discord_push',
    '{"action": "push", "message": "📅 Busy day ahead: {today_event_count} events today. Next: {next_event_title} in {event_coming_in_minutes} min.", "priority": 1}',
    1, 1, 'script_engine', 'helios',
    'DM when there are more than 3 calendar events today',
    86400
);

-- 7. Anomaly — any module error triggers alert
-- (This one uses a special condition the engine will handle)
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, approved_by, description, cooldown_secs)
VALUES (
    'module_error_alert',
    'event',
    '{"event": "module_error"}',
    '',
    'discord_push',
    '{"action": "push", "message": "⚠️ Module failure detected. Check Helios logs.", "priority": 2}',
    2, 1, 'script_engine', 'helios',
    'Alert channel when any module fails',
    3600
);

INSERT OR IGNORE INTO schema_version (version, description) VALUES (8, 'Seed rules for weather, protein, gaming, calendar');
