-- =============================================================================
-- Phase 3 — Structured Event Timeline Infrastructure
-- =============================================================================
-- Normalizes all data sources into unified timeline events for deterministic,
-- explainable narrative reconstruction. Every event is grounded in real data.
--
-- timeline_events: one row per significant occurrence across all modules
-- event_links: typed, confidence-scored relationships between events
-- =============================================================================

CREATE TABLE IF NOT EXISTS timeline_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,          -- ISO-8601 UTC timestamp of the event
    event_type  TEXT    NOT NULL,          -- taxonomy: location_change, focus_change,
                                          --   activity_spike, mood_recorded, alert_fired,
                                          --   sleep_completed, gaming_session, weather_change,
                                          --   spotify_listen, protein_logged, metric_anomaly,
                                          --   correlation_found, health_metric, system_event,
                                          --   narrative_summary
    source_module TEXT  NOT NULL,         -- module or collector that produced this event
    importance   REAL   NOT NULL DEFAULT 0.5,  -- 0.0–1.0 composite score
    summary      TEXT   NOT NULL,         -- human-readable one-liner
    metadata     TEXT,                    -- JSON blob: module-specific context
    date_key     TEXT   NOT NULL,         -- YYYY-MM-DD (MDT boundary)
    created_at   TEXT   NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS event_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_event_id INTEGER NOT NULL REFERENCES timeline_events(id) ON DELETE CASCADE,
    target_event_id INTEGER NOT NULL REFERENCES timeline_events(id) ON DELETE CASCADE,
    link_type       TEXT    NOT NULL,     -- taxonomy: causes, correlates_with, precedes,
                                          --   contradicts, same_context, derived_from
    confidence      REAL    NOT NULL DEFAULT 0.5,  -- 0.0–1.0
    evidence        TEXT,                 -- JSON or text explaining why this link exists
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Indexes for timeline queries
CREATE INDEX IF NOT EXISTS idx_timeline_ts      ON timeline_events(ts);
CREATE INDEX IF NOT EXISTS idx_timeline_type    ON timeline_events(event_type);
CREATE INDEX IF NOT EXISTS idx_timeline_date    ON timeline_events(date_key);
CREATE INDEX IF NOT EXISTS idx_timeline_import  ON timeline_events(importance DESC);
CREATE INDEX IF NOT EXISTS idx_timeline_source  ON timeline_events(source_module);

-- Indexes for link traversal
CREATE INDEX IF NOT EXISTS idx_el_source    ON event_links(source_event_id);
CREATE INDEX IF NOT EXISTS idx_el_target    ON event_links(target_event_id);
CREATE INDEX IF NOT EXISTS idx_el_type_conf ON event_links(link_type, confidence DESC);

INSERT OR IGNORE INTO schema_version (version, applied_at, description)
VALUES (18, datetime('now'), 'Phase 3: timeline_events + event_links for structured event reconstruction');
