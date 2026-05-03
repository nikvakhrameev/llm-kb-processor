PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE resources (
    id                       TEXT PRIMARY KEY,        -- UUIDv4
    resource_type            TEXT NOT NULL,           -- web|pdf|md|youtube|text|voice|_lint|_synthesis_weekly
    status                   TEXT NOT NULL,           -- received|parsing|parsed|gating|approved|rejected|ingesting|done|failed

    -- Telegram metadata (NULL for system rows like _lint)
    telegram_chat_id         INTEGER,
    telegram_message_id      INTEGER,
    telegram_user_id         INTEGER,

    -- Resource content references
    source_url               TEXT,                    -- web, youtube
    original_file_path       TEXT,                    -- raw/inbox/...
    parsed_text_path         TEXT,                    -- raw/parsed/...
    inline_text              TEXT,                    -- text type only, NULL for files
    content_title            TEXT,                    -- best-effort title
    content_hash             TEXT,                    -- sha256 of parsed text, optional in MVP

    -- Quality gate
    quality_score            INTEGER,                 -- 0..100, NULL until gated
    quality_rationale        TEXT,
    quality_topics           TEXT,                    -- JSON array
    quality_gate_skipped     INTEGER NOT NULL DEFAULT 0,  -- bool, true if all retries failed

    -- Ingest
    ingest_commit_sha        TEXT,                    -- git SHA after ingest
    ingest_summary           TEXT,                    -- JSON: {pages_created:[], pages_updated:[], warnings:[]}
    ingest_log_entry         TEXT,                    -- markdown bullet for log.md

    -- Pipeline control
    retry_count              INTEGER NOT NULL DEFAULT 0,
    next_attempt_at          TEXT,                    -- ISO-8601, NULL = ready now
    error_message            TEXT,

    -- Notification
    notification_sent_at     TEXT,                    -- NULL until terminal reply sent

    -- Timestamps
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at             TEXT
);

CREATE INDEX idx_resources_status_attempt
    ON resources (status, next_attempt_at, created_at);

CREATE INDEX idx_resources_telegram
    ON resources (telegram_chat_id, telegram_message_id);

-- Append-only audit log of state transitions and notable events
CREATE TABLE events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id   TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    event_type    TEXT NOT NULL,           -- status_change|parser_error|gate_result|ingest_result|notify_sent|...
    payload       TEXT,                    -- JSON
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_events_resource ON events (resource_id, created_at);

-- Lightweight Telegram-message-id deduplication
CREATE UNIQUE INDEX idx_resources_tg_unique
    ON resources (telegram_chat_id, telegram_message_id)
    WHERE telegram_message_id IS NOT NULL;
