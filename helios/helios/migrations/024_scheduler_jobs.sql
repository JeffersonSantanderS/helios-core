CREATE TABLE IF NOT EXISTS scheduled_jobs (
  job_key TEXT PRIMARY KEY,
  cadence TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'America/Edmonton',
  enabled INTEGER NOT NULL DEFAULT 1,
  last_due_at TEXT,
  last_started_at TEXT,
  last_completed_at TEXT,
  last_status TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS job_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_key TEXT NOT NULL,
  due_at TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  status TEXT NOT NULL,
  error TEXT,
  metadata_json TEXT,
  FOREIGN KEY(job_key) REFERENCES scheduled_jobs(job_key)
);

CREATE INDEX IF NOT EXISTS idx_job_runs_key ON job_runs(job_key);
CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at);

INSERT OR IGNORE INTO schema_version (version, description) VALUES (24, 'Scheduler jobs and job runs tracking tables');