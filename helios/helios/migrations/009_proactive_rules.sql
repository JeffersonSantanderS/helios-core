-- Proactive Discord push rules for Helios-style reactivity

-- 1. Protein below 25% with <4 hours left in the day
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, created_at)
VALUES (
    'protein_urgent',
    'tick',
    '{"on_change": true}',
    'protein.percent < 25 AND protein.hours_remaining < 4',
    'push_routed',
    '{"action": "push_routed", "message": "Protein at {protein.grams}g ({protein.percent}%) with {protein.hours_remaining}h left. Time to eat!", "priority": 3}',
    3, 1, 'autoDream', datetime('now')
);

-- 2. Mac bridge stops reporting (focus data stale > 15 min during work hours)
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, created_at)
VALUES (
    'focus_stale',
    'tick',
    '{"on_change": true}',
    'focus.idle_seconds > 900',
    'push_routed',
    '{"action": "push_routed", "message": "You have been idle for {focus.idle_seconds}s. Want a focus prompt?", "priority": 2}',
    2, 1, 'autoDream', datetime('now')
);

-- 3. Weather alert: extreme conditions
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, created_at)
VALUES (
    'weather_alert',
    'tick',
    '{"on_change": true}',
    'weather.temp < -15 OR weather.temp > 35',
    'push_routed',
    '{"action": "push_routed", "message": "Extreme weather: {weather.temp}C today ({weather.condition})", "priority": 2}',
    2, 1, 'autoDream', datetime('now')
);

-- 4. Gaming session > 2 hours (wellness check)
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, created_at)
VALUES (
    'gaming_marathon',
    'tick',
    '{"on_change": true}',
    'gaming.duration_secs > 7200',
    'push_routed',
    '{"action": "push_routed", "message": "{gaming.duration_mins} min gaming session. Time for a stretch?", "priority": 1}',
    1, 1, 'autoDream', datetime('now')
);

-- 5. Dream cycle completed (push summary)
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, created_at)
VALUES (
    'dream_cycle_complete',
    'schedule',
    '{"after_dream": true}',
    'true',
    'push',
    '{"action": "push", "message": "autoDream completed - patterns identified and context pruned.", "priority": 1}',
    1, 1, 'autoDream', datetime('now')
);

INSERT OR IGNORE INTO schema_version (version, description) VALUES (9, 'Proactive push rules for protein, focus, weather, gaming, dream');