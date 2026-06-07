-- ============================================================================
-- Helios v6 — Nutrition Rules (Migration 023)
-- ============================================================================
-- Calorie and nutrition-focused rules replacing old SparkyFitness-specific
-- rules. These use the nutrition module context (calories_today, calorie_pct,
-- protein_today, protein_pct, etc.).
-- ============================================================================

-- 1. Calories behind — DM if under 25% of target by afternoon
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, approved_by, description, cooldown_secs)
VALUES (
    'calories_behind_afternoon',
    'threshold',
    '{}',
    'nutrition.calorie_pct < 25 AND nutrition.entries_today > 0',
    'push_routed',
    '{"action": "push_routed", "message": "🍽️ Low calories: only {nutrition.calorie_pct}% of your {nutrition.calorie_target} target ({nutrition.calories_today} cal so far). Time to eat!", "priority": 1, "title": "Low Calories", "category": "nutrition"}',
    1, 1, 'script_engine', 'helios',
    'Alert when calories are under 25% of target after entries exist',
    14400
);

-- 2. Calories approaching target — informational nudge
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, approved_by, description, cooldown_secs)
VALUES (
    'calories_approaching',
    'threshold',
    '{}',
    'nutrition.calorie_pct >= 80 AND nutrition.calorie_pct < 100',
    'push_routed',
    '{"action": "push_routed", "message": "_almost there! {nutrition.calories_today}/{nutrition.calorie_target} cal ({nutrition.calorie_pct}%). {nutrition.calories_remaining} cal remaining.", "priority": 1, "title": "Calories Almost There", "category": "nutrition"}',
    1, 1, 'script_engine', 'helios',
    'Notify when calories reach 80%+ of daily target',
    14400
);

-- 3. Calories exceeded — heads up
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, approved_by, description, cooldown_secs)
VALUES (
    'calories_exceeded',
    'threshold',
    '{}',
    'nutrition.calorie_pct >= 100',
    'push_routed',
    '{"action": "push_routed", "message": "🔥 Calorie target reached: {nutrition.calories_today}/{nutrition.calorie_target} cal ({nutrition.calorie_pct}%). Good intake today!", "priority": 2, "title": "Calorie Target Met", "category": "nutrition"}',
    2, 1, 'script_engine', 'helios',
    'Congratulate when calorie target is met or exceeded',
    28800
);

-- 4. Protein behind — updated from old rule, now uses nutrition module context
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, approved_by, description, cooldown_secs)
VALUES (
    'protein_behind_nutrition',
    'threshold',
    '{}',
    'nutrition.protein_pct < 50',
    'push_routed',
    '{"action": "push_routed", "message": "🥩 Protein check: only {nutrition.protein_pct}% of your {nutrition.protein_target}g target ({nutrition.protein_today}g today). Time to eat!", "priority": 1, "title": "Protein Behind", "category": "nutrition"}',
    1, 1, 'script_engine', 'helios',
    'DM when protein intake is below 50% of daily target (nutrition module)',
    7200
);

-- 5. Protein goal met — updated from old rule
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition, action_type, action_config, priority, enabled, created_by, approved_by, description, cooldown_secs)
VALUES (
    'protein_goal_nutrition',
    'threshold',
    '{}',
    'nutrition.protein_pct >= 100',
    'push_routed',
    '{"action": "push_routed", "message": "💪 Protein goal met! {nutrition.protein_today}g / {nutrition.protein_target}g ({nutrition.protein_pct}%). Nice work!", "priority": 2, "title": "Protein Goal Met", "category": "nutrition"}',
    2, 1, 'script_engine', 'helios',
    'Congratulate when protein goal is reached (nutrition module)',
    28800
);

INSERT OR IGNORE INTO schema_version (version, description) VALUES (23, 'Nutrition-focused rules for calorie and protein tracking');