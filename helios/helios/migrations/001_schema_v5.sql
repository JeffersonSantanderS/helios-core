-- Helios v5 Schema Migration
-- Creates all tables and indexes for the hybrid architecture
-- Script engine → writes to context/llm_requests | LLM → writes to context/decisions
-- Rule approval gate: rules with created_by='llm_suggested' require approved_by IS NOT NULL AND enabled=1 (allows INSERT with enabled=0)

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- ============================================================================
-- context: Shared state packets between script engine and LLM bridge
-- ============================================================================
CREATE TABLE IF NOT EXISTS context (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    source      TEXT    NOT NULL,                -- 'script_engine' or 'llm_bridge'
    module      TEXT    NOT NULL,                -- Module name (weather, mood, focus, etc.)
    key         TEXT    NOT NULL,                -- State key within module
    value       TEXT    NOT NULL DEFAULT '{}',    -- JSON value payload
    priority    INTEGER NOT NULL DEFAULT 0,      -- 0=normal, 1=high, 2=critical
    expires_at  TEXT,                             -- Optional TTL (ISO8601)
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT ctx_unique_latest UNIQUE (module, key, source)
) ;

-- Indexes for context query patterns
CREATE INDEX IF NOT EXISTS idx_context_module_ts     ON context (module, ts);
CREATE INDEX IF NOT EXISTS idx_context_source_ts     ON context (source, ts);
CREATE INDEX IF NOT EXISTS idx_context_priority       ON context (priority DESC);
CREATE INDEX IF NOT EXISTS idx_context_expires       ON context (expires_at) WHERE expires_at IS NOT NULL;

-- ============================================================================
-- llm_requests: Async queue for LLM handoffs
-- ============================================================================
-- Script engine writes request rows (status='pending')
-- LLM bridge picks up pending requests, processes them, updates status+result
CREATE TABLE IF NOT EXISTS llm_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    request_type    TEXT    NOT NULL,                -- 'summary', 'decision', 'analysis', 'generation'
    context_keys    TEXT    NOT NULL DEFAULT '[]',    -- JSON array of context keys to include
    prompt_template TEXT,                             -- Custom prompt template (overrides default)
    max_tokens      INTEGER NOT NULL DEFAULT 2048,
    priority        INTEGER NOT NULL DEFAULT 0,       -- 0=normal, 1=high, 2=critical
    status          TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'done', 'failed')),
    result          TEXT,                             -- JSON result from LLM
    result_ts       TEXT,                             -- When result was received
    error           TEXT,                             -- Error message if failed
    model_used      TEXT,                             -- Which model processed this request
    retry_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Indexes for LLM request queue processing
CREATE INDEX IF NOT EXISTS idx_llm_req_status_priority ON llm_requests (status, priority DESC, ts);
CREATE INDEX IF NOT EXISTS idx_llm_req_type_ts         ON llm_requests (request_type, ts);
CREATE INDEX IF NOT EXISTS idx_llm_req_ts               ON llm_requests (ts);

-- ============================================================================
-- decisions: Audit trail for all decisions (script engine + LLM)
-- ============================================================================
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decision_type   TEXT    NOT NULL,                -- 'rule_trigger', 'llm_reasoning', 'user_override', 'circuit_breaker'
    source          TEXT    NOT NULL,                -- 'script_engine', 'llm_bridge', 'user', 'system'
    context         TEXT    NOT NULL DEFAULT '{}',    -- JSON: relevant context at decision time
    action          TEXT    NOT NULL,                -- What action was taken
    outcome         TEXT,                             -- Result of the action (nullable until resolved)
    module          TEXT,                             -- Which module this decision relates to
    rule_id         TEXT,                             -- FK to rules table (if rule-triggered)
    confidence      REAL,                             -- LLM confidence score (0.0-1.0)
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT fk_decision_rule FOREIGN KEY (rule_id) REFERENCES rules (slug)
);

-- Indexes for decision audit queries
CREATE INDEX IF NOT EXISTS idx_decisions_type_ts     ON decisions (decision_type, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_source_ts   ON decisions (source, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_module_ts    ON decisions (module, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_rule         ON decisions (rule_id) WHERE rule_id IS NOT NULL;

-- ============================================================================
-- rules: Dynamic rules with approval flow
-- ============================================================================
-- Rules where created_by='llm_suggested' MUST have approved_by IS NOT NULL AND enabled=1 (pending LLM rules can be INSERTED with enabled=0)
CREATE TABLE IF NOT EXISTS rules (
    slug            TEXT    PRIMARY KEY,                -- Human-readable unique ID (e.g., 'morning_weather_alert')
    trigger_type    TEXT    NOT NULL,                     -- 'schedule', 'threshold', 'pattern', 'event'
    trigger_config  TEXT    NOT NULL DEFAULT '{}',        -- JSON: trigger configuration (cron, thresholds, patterns)
    condition       TEXT,                                 -- SQL-like condition expression against context state
    action_type     TEXT    NOT NULL,                     -- 'notify', 'llm_request', 'state_update', 'discord_push'
    action_config   TEXT    NOT NULL DEFAULT '{}',        -- JSON: action parameters
    priority        INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,           -- 1=active, 0=disabled
    created_by      TEXT    NOT NULL,                      -- 'script_engine', 'llm_suggested', 'user'
    approved_by     TEXT,                                 -- NULL if pending approval, set when approved
    category        TEXT    NOT NULL DEFAULT '',           -- v6: rule category for routing
    severity        TEXT    NOT NULL DEFAULT 'info',       -- v6: info/warning/critical
    description    TEXT,                                 -- Human-readable description
    cooldown_secs   INTEGER NOT NULL DEFAULT 300,         -- Minimum seconds between triggers
    last_triggered  TEXT,                                 -- Timestamp of last trigger
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    -- Approval constraint: LLM-suggested rules require explicit approval
    CONSTRAINT chk_llm_approval CHECK (
        created_by != 'llm_suggested' OR (approved_by IS NOT NULL AND enabled = 1)
    )
);

-- Indexes for rules evaluation
CREATE INDEX IF NOT EXISTS idx_rules_trigger_type    ON rules (trigger_type, enabled);
CREATE INDEX IF NOT EXISTS idx_rules_enabled_priority ON rules (enabled, priority DESC);

-- ============================================================================
-- mood: Daily mood check-ins with emoji scale
-- ============================================================================
CREATE TABLE IF NOT EXISTS mood (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    emoji           TEXT    NOT NULL,                -- Emoji representing mood (e.g., '😊', '😐', '😢')
    score           INTEGER NOT NULL CHECK (score BETWEEN 1 AND 10),
    note            TEXT,                             -- Optional text note
    source          TEXT    NOT NULL DEFAULT 'discord_button',  -- 'discord_button', 'manual', 'llm_inferred'
    discord_msg_id  TEXT,                             -- Discord message ID for button interaction
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Indexes for mood queries
CREATE INDEX IF NOT EXISTS idx_mood_ts            ON mood (ts);
CREATE INDEX IF NOT EXISTS idx_mood_score_ts      ON mood (score, ts);

-- ============================================================================
-- focus: Productivity tracking with calendar-aware work hours + gaming detection
-- ============================================================================
CREATE TABLE IF NOT EXISTS focus (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    state           TEXT    NOT NULL CHECK (state IN ('working', 'gaming', 'idle', 'meeting', 'break')),
    source          TEXT    NOT NULL,                -- 'calendar', 'gaming_detection', 'manual'
    context         TEXT    NOT NULL DEFAULT '{}',    -- JSON: what game/app, meeting title, etc.
    duration_secs   INTEGER,                         -- Duration in this state (if snapshot-style)
    session_start   TEXT,                             -- When this focus session started
    session_end     TEXT,                             -- When it ended (NULL if ongoing)
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Indexes for focus queries
CREATE INDEX IF NOT EXISTS idx_focus_state_ts      ON focus (state, ts);
CREATE INDEX IF NOT EXISTS idx_focus_source_ts     ON focus (source, ts);
CREATE INDEX IF NOT EXISTS idx_focus_session       ON focus (session_start, session_end) WHERE session_end IS NULL;

-- ============================================================================
-- habits: Streak tracking
-- ============================================================================
CREATE TABLE IF NOT EXISTS habits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT    NOT NULL UNIQUE,          -- e.g., 'morning_stretch', 'meditation'
    description     TEXT,
    frequency       TEXT    NOT NULL DEFAULT 'daily', -- 'daily', 'weekly', 'custom'
    target_count    INTEGER NOT NULL DEFAULT 1,        -- Times per period
    current_streak  INTEGER NOT NULL DEFAULT 0,
    longest_streak  INTEGER NOT NULL DEFAULT 0,
    last_completed  TEXT,                             -- ISO8601 timestamp of last completion
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS habit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id        INTEGER NOT NULL REFERENCES habits (id),
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    note            TEXT,                             -- Optional note about this completion
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Indexes for habit queries
CREATE INDEX IF NOT EXISTS idx_habits_slug            ON habits (slug);
CREATE INDEX IF NOT EXISTS idx_habit_log_habit_ts     ON habit_log (habit_id, ts);

-- ============================================================================
-- tasks: Task list with pyicloud sync metadata
-- ============================================================================
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT    NOT NULL,
    description     TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'done', 'cancelled')),
    priority        INTEGER NOT NULL DEFAULT 0,       -- 0=normal, 1=high, 2=critical
    source          TEXT    NOT NULL DEFAULT 'manual',  -- 'manual', 'pyicloud', 'llm_suggested'
    icloud_id       TEXT,                             -- iCloud Reminders ID for sync
    icloud_list     TEXT,                             -- iCloud list name
    due_date        TEXT,                             -- ISO8601 due date
    completed_at    TEXT,                             -- When task was completed
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT chk_task_icloud UNIQUE (icloud_id)
);

-- Indexes for task queries
CREATE INDEX IF NOT EXISTS idx_tasks_status_priority  ON tasks (status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_source           ON tasks (source);
CREATE INDEX IF NOT EXISTS idx_tasks_due_date         ON tasks (due_date) WHERE due_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_icloud           ON tasks (icloud_id) WHERE icloud_id IS NOT NULL;

-- ============================================================================
-- Schema version tracking
-- ============================================================================
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    description TEXT
);

INSERT OR IGNORE INTO schema_version (version, description) VALUES (1, 'Initial v5 schema — context, llm_requests, decisions, rules, mood, focus, habits, tasks');