# Data Model

## Filesystem layout

```
project_root/
├── state.db                 # SQLite, single file, WAL mode
├── knowledge_base/          # git working tree
│   ├── CLAUDE.md            # ingest/lint/synthesis rules for the agent
│   ├── purpose.md           # what belongs in this wiki, in plain English
│   ├── index.md             # auto-maintained list of all wiki pages
│   ├── log.md               # append-only chronological event log
│   ├── raw/
│   │   ├── inbox/           # original files as received (pdf, ogg, ...)
│   │   ├── parsed/
│   │   │   ├── web/
│   │   │   ├── pdf/
│   │   │   ├── youtube/
│   │   │   ├── text/
│   │   │   └── voice/
│   │   └── rejected/        # quality-gated out, kept for audit
│   └── wiki/
│       ├── entities/        # one page per person, company, product, tool
│       ├── concepts/        # one page per idea, method, theory
│       ├── sources/         # one page per ingested source
│       └── syntheses/
│           ├── weekly/      # YYYY-WW.md
│           └── topics/      # ad-hoc topic syntheses
└── docs/                    # this technical spec (not under wiki git)
```

The `knowledge_base/` directory is its **own** git repository, separate from
the application code. The application code lives one level up. Auto-push to a
remote (e.g. private GitHub repo) is configured via a post-commit hook in
`knowledge_base/.git/hooks/`.

## SQLite schema

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE resources (
    id                       TEXT PRIMARY KEY,        -- UUIDv4
    resource_type            TEXT NOT NULL,           -- web|pdf|md|youtube|text|voice|_lint|_synthesis_weekly
    status                   TEXT NOT NULL,           -- see 03-pipeline-states.md

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
-- (a single Telegram message should produce at most one resources row)
CREATE UNIQUE INDEX idx_resources_tg_unique
    ON resources (telegram_chat_id, telegram_message_id)
    WHERE telegram_message_id IS NOT NULL;
```

### Why a single `resources` table

All resource types share the same lifecycle: receive → parse → gate → ingest →
done. Type-specific data (URL, file path, transcript path) is sparse and fits
naturally as nullable columns. A polymorphic schema with separate tables would
multiply joins and complicate the polling queries with no real benefit.

System rows for cron jobs (`resource_type='_lint'`,
`resource_type='_synthesis_weekly'`) reuse the same table because they go
through the same ingest worker. They skip the parsing and gating stages
(status starts at `approved`) and use a different prompt template inside the
ingest worker.

### What is NOT in SQLite

- Parsed text content. It lives on disk in `raw/parsed/`. SQLite holds the
  path only.
- Wiki content. Lives in `knowledge_base/wiki/`, version-controlled by git.
- Quality gate raw responses (only score/rationale/topics are persisted; the
  full prompt and response can optionally be logged to `events.payload`).
- Telegram bot token, API keys. Environment variables only.

## Frontmatter conventions

### `raw/parsed/<type>/<slug>.md`

Every parsed source is a markdown file with YAML frontmatter:

```yaml
---
resource_id: 0193f7a8-3c8b-7e2a-9f4c-2c9e7d3a1b6e
resource_type: web
source_url: https://example.com/great-article
title: "Great Article on Foo"
fetched_at: 2026-05-02T14:30:00Z
char_count: 18452
parser: trafilatura@2.x
---

# Great Article on Foo

(parsed body here, in clean markdown)
```

### `wiki/sources/<slug>.md`

The agent creates one page per ingested source. Conventions in
`10-knowledge-base.md`. Always includes a back-reference to the parsed file:

```yaml
---
resource_id: 0193f7a8-3c8b-7e2a-9f4c-2c9e7d3a1b6e
parsed_path: raw/parsed/web/0193f7a8-great-article.md
ingested_at: 2026-05-02T14:32:11Z
---
```

### `wiki/entities/<Name>.md`, `wiki/concepts/<name>.md`

Owned by the agent. Section structure is enforced by CLAUDE.md (see
`10-knowledge-base.md`). The agent appends; it does not rewrite from scratch.

## File naming

- `slug` is `<resource_id_short>-<kebab-case-title>` truncated to 60 chars.
- Resource IDs are UUIDv4. Short form is the first 8 hex chars.
- Wiki entity pages use Title Case with hyphens: `Andrej-Karpathy.md`.
- Wiki concept pages use lowercase kebab-case: `mixture-of-experts.md`.

## Git conventions inside `knowledge_base/`

Commit message prefixes are mandatory and parsed by the workers:

| Prefix       | Author          | Trigger                              |
|--------------|-----------------|--------------------------------------|
| `ingest:`    | Claude agent    | Successful source ingestion          |
| `lint:`      | Claude agent    | Daily lint cleanup                   |
| `synthesis:` | Claude agent    | Weekly or topic synthesis            |
| `manual:`    | Human (you)     | Hand edits via Obsidian or local IDE |

The ingest worker verifies that the latest commit after a Claude run starts
with the expected prefix. A commit not matching the prefix indicates the agent
did something off-script and is treated as a failure.
