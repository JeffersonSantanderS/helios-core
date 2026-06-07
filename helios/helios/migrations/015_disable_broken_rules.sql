-- ============================================================================
-- Helios v6 — Disable rules referencing non-existent module keys (PR 3)
-- ============================================================================
-- Rules disabled because module output keys don't match:
--   server.*    — server module exists but not enabled in config
--   system.*    — system_health module manifest name is 'system' but config uses 'system_health'
--   gaming.*    — GamingModule returns {active_games, count, is_gaming}, not duration
--   location.*  — LocationModule returns {source, province, city, lat, lon, accuracy, device}
--   health.*    — HealthModule returns {records, latest, last_sync, sync_status, ...}
--
-- Rules KEPT enabled:
--   morning_checkin, low_sleep_alert, spotify_token_expired, no_focus_detected
-- ============================================================================

-- SERVER HEALTH: server module not enabled in config
UPDATE rules SET enabled=0 WHERE slug='high_cpu_alert';

-- DATA FRESHNESS: location module doesn't produce a `stale` key
UPDATE rules SET enabled=0 WHERE slug='location_stale';

-- DATA FRESHNESS: health module doesn't produce sync_stale or hours_since_sync
UPDATE rules SET enabled=0 WHERE slug='health_sync_stale';

-- LOCATION: location module doesn't produce geofence keys (at_home, previous_at_home, dist_from_home_km)
UPDATE rules SET enabled=0 WHERE slug='left_home_zone';
UPDATE rules SET enabled=0 WHERE slug='arrived_home';

-- HEALTH: health module doesn't produce hr_spike or hr_baseline keys
UPDATE rules SET enabled=0 WHERE slug='resting_hr_spike';

-- BEHAVIORAL: gaming module returns {active_games, count, is_gaming}, not duration
UPDATE rules SET enabled=0 WHERE slug='gaming_extended';

-- SYSTEM: system_health module not loaded (manifest name mismatch in config)
UPDATE rules SET enabled=0 WHERE slug='collector_crash';
UPDATE rules SET enabled=0 WHERE slug='db_size_warning';
UPDATE rules SET enabled=0 WHERE slug='tick_failure_cascade';

-- SCHEDULED: evening_wrap references gaming.duration_mins (module doesn't produce)
UPDATE rules SET enabled=0 WHERE slug='evening_wrap';

-- ANOMALY: references system.anomaly_count (system module not loaded)
UPDATE rules SET enabled=0 WHERE slug='anomaly_detected';

INSERT OR IGNORE INTO schema_version (version, description) VALUES (15, 'Disable rules referencing non-existent module keys (PR 3 hardening)');
