-- Delivery ledger: audit trail for outbound notifications
CREATE TABLE IF NOT EXISTS delivery_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_fingerprint TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_category TEXT,
  event_source TEXT,
  event_priority INTEGER DEFAULT 2,
  route TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  success INTEGER NOT NULL DEFAULT 0,
  response_detail TEXT,
  error_detail TEXT,
  matrix_event_id TEXT,
  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_delivery_fingerprint ON delivery_attempts(event_fingerprint);
CREATE INDEX IF NOT EXISTS idx_delivery_ts ON delivery_attempts(ts);
CREATE INDEX IF NOT EXISTS idx_delivery_channel ON delivery_attempts(channel_name);

INSERT OR IGNORE INTO schema_version (version, description) VALUES (25, 'Delivery audit trail for outbound notifications');