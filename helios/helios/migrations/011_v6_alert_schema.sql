-- ============================================================================
-- Helios v6 — Alert schema migration
-- ============================================================================
-- alert_history: persistent alert log (survives restart)
-- alert_snoozes: user-snoosed rules with expiration
-- ============================================================================

CREATE TABLE IF NOT EXISTS alert_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_slug   TEXT    NOT NULL,
    ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    severity    TEXT    NOT NULL DEFAULT 'info',
    category    TEXT    NOT NULL DEFAULT 'system',
    message     TEXT    NOT NULL,
    sent        INTEGER NOT NULL DEFAULT 1,
    context     TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_alert_history_ts      ON alert_history (ts);
CREATE INDEX IF NOT EXISTS idx_alert_history_severity ON alert_history (severity, ts);
CREATE INDEX IF NOT EXISTS idx_alert_history_rule     ON alert_history (rule_slug, ts);
CREATE INDEX IF NOT EXISTS idx_alert_history_category ON alert_history (category, ts);

CREATE TABLE IF NOT EXISTS alert_snoozes (
    rule_slug   TEXT    PRIMARY KEY,
    snoozed_until TEXT NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT OR IGNORE INTO schema_version (version, description) VALUES (11, 'v6 alert_history + alert_snoozes tables');
