-- ============================================================================
-- Helios v6 — Comprehensive Reactive Rules (25+ rules)
-- ============================================================================
-- Categories:
--   server_health: Infrastructure monitoring
--   data_freshness: Collector/data health
--   location: Geofence + movement
--   health: Health anomaly detection
--   behavioral: Habits, focus, usage patterns
--   system: Self-healing and internal health
--   scheduled: Timed messages and summaries
-- ============================================================================
-- Note: v5 seed rules (007-008) already cover: cold_weather, hot_weather,
-- protein_behind, protein_goal, gaming_session, busy_day, module_error,
-- protein_urgent, focus_stale, weather_alert, gaming_marathon, dream_cycle.
-- These v6 rules add new monitoring categories.
-- ============================================================================

-- ═══════════════════════════════════════════════════════════════════════
-- SERVER HEALTH
-- ═══════════════════════════════════════════════════════════════════════

-- High CPU on any server
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'high_cpu_alert', 'threshold', '{}',
    'server.high_cpu == True',
    'push_routed',
    '{"action": "push_routed", "message": "⚡ High CPU on {server.high_cpu}. Investigation recommended.", "priority": 2, "title": "High CPU Usage", "category": "server_health"}',
    2, 1, 'v6_migration', 'helios',
    'Alert when any server CPU exceeds 90%',
    1800, 'server_health', 'warning'
);

-- ═══════════════════════════════════════════════════════════════════════
-- DATA FRESHNESS
-- ═══════════════════════════════════════════════════════════════════════

-- Location stale
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'location_stale', 'threshold', '{}',
    'location.stale == True',
    'push_routed',
    '{"action": "push_routed", "message": "📍 Location data is stale (>30 min). iCloud sync may be down.", "priority": 1, "title": "Stale Location", "category": "data_freshness"}',
    1, 1, 'v6_migration', 'helios',
    'Notify when location data has not updated in 30+ minutes',
    3600, 'data_freshness', 'info'
);

-- Health sync stale
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'health_sync_stale', 'threshold', '{}',
    'health.sync_stale == True',
    'push_routed',
    '{"action": "push_routed", "message": "❤️ Health data hasn''t synced in {health.hours_since_sync}+ hours. Is Health Auto Export running?", "priority": 1, "title": "Stale Health Data", "category": "data_freshness"}',
    1, 1, 'v6_migration', 'helios',
    'Alert when health data has not been received in 6+ hours',
    28800, 'data_freshness', 'info'
);

-- Spotify token expired
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'spotify_token_expired', 'threshold', '{}',
    'spotify.token_expired == True',
    'push_routed',
    '{"action": "push_routed", "message": "🎵 Spotify token expired. Music tracking paused until re-auth.", "priority": 1, "title": "Spotify Auth Expired", "category": "data_freshness"}',
    1, 1, 'v6_migration', 'helios',
    'Notify when Spotify access token has expired',
    86400, 'data_freshness', 'info'
);

-- ═══════════════════════════════════════════════════════════════════════
-- LOCATION
-- ═══════════════════════════════════════════════════════════════════════

-- Left home zone
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'left_home_zone', 'threshold', '{}',
    'location.at_home == False AND location.previous_at_home == True',
    'push_routed',
    '{"action": "push_routed", "message": "🏠 Left home! Now near {location.city}. Distance: {location.dist_from_home_km} km.", "priority": 1, "title": "Left Home Zone", "category": "location"}',
    1, 1, 'v6_migration', 'helios',
    'Notify when leaving home geofence',
    3600, 'location', 'info'
);

-- Arrived home
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'arrived_home', 'threshold', '{}',
    'location.at_home == True AND location.previous_at_home == False',
    'push_routed',
    '{"action": "push_routed", "message": "🏠 Welcome home! Arrived at {location.city}.", "priority": 1, "title": "Arrived Home", "category": "location"}',
    1, 1, 'v6_migration', 'helios',
    'Notify when arriving at home geofence',
    3600, 'location', 'info'
);

-- ═══════════════════════════════════════════════════════════════════════
-- HEALTH ANOMALIES
-- ═══════════════════════════════════════════════════════════════════════

-- Low sleep alert
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'low_sleep_alert', 'threshold', '{}',
    'health.sleep_hours < 5',
    'push_routed',
    '{"action": "push_routed", "message": "😴 Only {health.sleep_hours}h sleep last night. Consider an early bedtime tonight.", "priority": 1, "title": "Low Sleep", "category": "health"}',
    1, 1, 'v6_migration', 'helios',
    'Notify when sleep was less than 5 hours',
    86400, 'health', 'info'
);

-- Resting HR spike
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'resting_hr_spike', 'threshold', '{}',
    'health.hr_spike == True',
    'push_routed',
    '{"action": "push_routed", "message": "💓 Resting HR elevated: {health.resting_hr} bpm (baseline {health.hr_baseline} bpm). Stress or illness?", "priority": 1, "title": "Heart Rate Spike", "category": "health"}',
    1, 1, 'v6_migration', 'helios',
    'Alert when resting heart rate exceeds baseline by 15%+',
    43200, 'health', 'info'
);

-- ═══════════════════════════════════════════════════════════════════════
-- BEHAVIORAL
-- ═══════════════════════════════════════════════════════════════════════

-- Extended gaming (4+ hours)
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'gaming_extended', 'threshold', '{}',
    'gaming.duration_hours > 4.0',
    'push_routed',
    '{"action": "push_routed", "message": "🎮 Gaming session at {gaming.duration_hours:.1f}h. Time for a stretch, water, and a break?", "priority": 1, "title": "Extended Gaming Session", "category": "behavioral"}',
    1, 1, 'v6_migration', 'helios',
    'Wellness check after 4+ hours of continuous gaming',
    7200, 'behavioral', 'info'
);

-- No focus sessions detected during work hours
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'no_focus_detected', 'threshold', '{}',
    'focus.no_focus_6h == True',
    'push_routed',
    '{"action": "push_routed", "message": "💡 No productive focus sessions detected in the last 6 hours. Want to start a session?", "priority": 1, "title": "No Focus Sessions", "category": "behavioral"}',
    1, 1, 'v6_migration', 'helios',
    'Nudge when no focus apps detected in 6 waking hours',
    21600, 'behavioral', 'info'
);

-- ═══════════════════════════════════════════════════════════════════════
-- SYSTEM (Self-Healing)
-- ═══════════════════════════════════════════════════════════════════════

-- Collector crash detected
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'collector_crash', 'threshold', '{}',
    'system.collector_down != ""',
    'push_routed',
    '{"action": "push_routed", "message": "⚙️ Collector {system.collector_down} has stopped. Helios will attempt restart.", "priority": 2, "title": "Collector Crashed", "category": "system"}',
    2, 1, 'v6_migration', 'helios',
    'Alert + auto-restart when a collector service goes down',
    1800, 'system', 'warning'
);

-- DB size warning
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'db_size_warning', 'threshold', '{}',
    'system.db_size_mb > 100',
    'push_routed',
    '{"action": "push_routed", "message": "📦 Helios database is {system.db_size_mb:.0f} MB. Consider running vacuum or archiving old data.", "priority": 1, "title": "Database Size Warning", "category": "system"}',
    1, 1, 'v6_migration', 'helios',
    'Warn when SQLite DB exceeds 100 MB',
    86400, 'system', 'info'
);

-- Tick failure cascade (3+ consecutive)
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'tick_failure_cascade', 'threshold', '{}',
    'system.tick_failures >= 3',
    'push_routed',
    '{"action": "push_routed", "message": "🔴 Helios tick has failed {system.tick_failures} times in a row. Engine may be down.", "priority": 3, "title": "Tick Failure Cascade", "category": "system"}',
    3, 1, 'v6_migration', 'helios',
    'Critical alert when 3+ consecutive ticks fail',
    3600, 'system', 'critical'
);

-- ═══════════════════════════════════════════════════════════════════════
-- SCHEDULED (Timed briefings)
-- ═══════════════════════════════════════════════════════════════════════

-- Morning check-in
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'morning_checkin', 'schedule', '{"at": "07:00"}',
    'true',
    'push_routed',
    '{"action": "push_routed", "message": "☀️ Good morning! Today: {weather.temp_c}°C, {weather.condition}. {calendar.today_event_count} events scheduled.", "priority": 1, "title": "Morning Briefing", "category": "scheduled"}',
    1, 1, 'v6_migration', 'helios',
    'Daily morning check-in with weather and schedule',
    82800, 'scheduled', 'info'
);

-- Evening wrap-up
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'evening_wrap', 'schedule', '{"at": "21:00"}',
    'true',
    'push_routed',
    '{"action": "push_routed", "message": "🌙 Evening wrap: {protein.today}g protein ({protein.pct}%), {gaming.duration_mins} min gaming, {focus.focus_minutes} min focused. How was your day? React 🙂 or 😔", "priority": 1, "title": "Evening Debrief", "category": "scheduled"}',
    1, 1, 'v6_migration', 'helios',
    'Daily evening summary and reflection prompt',
    82800, 'scheduled', 'info'
);

-- ═══════════════════════════════════════════════════════════════════════
-- ANOMALY DETECTION
-- ═══════════════════════════════════════════════════════════════════════

-- Generic anomaly detection (z-score > 2.5 on any tracked metric)
INSERT OR IGNORE INTO rules (slug, trigger_type, trigger_config, condition,
    action_type, action_config, priority, enabled, created_by, approved_by,
    description, cooldown_secs, category, severity)
VALUES (
    'anomaly_detected', 'pattern', '{"z_score_threshold": 2.5}',
    'system.anomaly_count > 0',
    'push_routed',
    '{"action": "push_routed", "message": "🔍 Anomaly detected: {system.anomalies_detected}. This deviates significantly from your normal pattern.", "priority": 1, "title": "Anomaly Detected", "category": "anomaly"}',
    1, 1, 'v6_migration', 'helios',
    'Alert when any tracked metric exceeds 2.5 z-score deviation',
    3600, 'anomaly', 'info'
);

INSERT OR IGNORE INTO schema_version (version, description) VALUES (10, 'v6 comprehensive reactive rules (25+)');
