# Architecture

## Process topology

The system runs as four long-lived Python processes, plus the Claude Agent SDK
which is invoked as a subprocess by the ingest worker (inside its own Docker
container). All four processes share the same SQLite database and the same
`knowledge_base/` git working tree (read-only for some, read-write for others).

```
┌──────────────────────────────────────────────────────────────────┐
│                          host VPS                                │
│                                                                  │
│  ┌──────────┐    ┌───────────────────┐    ┌──────────────────┐   │
│  │  bot     │    │ resource-worker   │    │ ingest-worker    │   │
│  │ aiogram  │    │  - parsers        │    │  - claude SDK    │   │
│  │ polling  │    │  - quality gate   │    │  - docker exec   │   │
│  └────┬─────┘    └──────────┬────────┘    └────────┬─────────┘   │
│       │                     │                      │             │
│       │                     │                      │             │
│       ▼                     ▼                      ▼             │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │                    state.db (SQLite, WAL)                │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ┌──────────┐                                                    │
│  │scheduler │  triggers daily lint + weekly synthesis            │
│  │APSched.  │  → enqueues special rows in resources table        │
│  └──────────┘                                                    │
│                                                                  │
│  ┌────────────────────────────────────────────────────────┐      │
│  │           knowledge_base/  (git working tree)          │      │
│  │  - bot writes to raw/inbox/                            │      │
│  │  - resource-worker writes to raw/parsed|rejected/      │      │
│  │  - ingest-worker (in its container) writes to wiki/    │      │
│  └────────────────────────────────────────────────────────┘      │
└──────────────────────────────────────────────────────────────────┘
```

## Component contracts

### Bot

- Long-poll Telegram updates with aiogram.
- For every incoming message:
  1. Allowlist check on `from_user.id`. Silent drop if not allowed.
  2. Type detection (see `04-telegram-bot.md`).
  3. If unsupported: reply with reason, do not persist.
  4. If supported: download attachment to `raw/inbox/<uuid>.<ext>` (for files
     and voice) or store text inline in the SQLite row.
  5. Insert a `resources` row with `status='received'` and all Telegram
     metadata (chat_id, message_id, user_id).
  6. Reply to the original message with a short "queued" ack including the
     resource id.

The bot **does not** call any LLM and **does not** parse content. Its sole
responsibility is intake.

### Resource worker

- Single process, single asyncio task loop.
- Polls SQLite every `POLL_INTERVAL_SECONDS` (default 2s):
  ```sql
  SELECT id FROM resources
   WHERE status IN ('received', 'parsed')
     AND (next_attempt_at IS NULL OR next_attempt_at <= now())
   ORDER BY created_at
   LIMIT 1;
  ```
- For `received` rows: dispatch to the parser matching `resource_type`. Write
  parsed text to `raw/parsed/<type>/<slug>.md` with a YAML frontmatter
  containing all metadata. Update row to `parsed`.
- For `parsed` rows: run the DeepSeek quality gate. Update row to `approved`
  (and notify ingest worker by leaving status pollable) or `rejected` (and
  send Telegram reply).
- On any exception: bump `retry_count`, set `next_attempt_at` to
  `now + backoff(retry_count)`, set `error_message`. After 3 retries set
  status to terminal `failed` and notify.

The resource worker **does not** touch the wiki and **does not** invoke
Claude.

### Ingest worker

- Single process, single asyncio task loop. **Important**: at most one ingest
  runs at a time. Concurrent ingests would race on the git working tree.
- Polls SQLite every `POLL_INTERVAL_SECONDS`:
  ```sql
  SELECT id FROM resources
   WHERE status = 'approved'
     AND (next_attempt_at IS NULL OR next_attempt_at <= now())
   ORDER BY created_at
   LIMIT 1;
  ```
- Sets status to `ingesting`, then launches Claude Agent SDK against the
  parsed text (see `07-ingest-agent.md`).
- Captures the `report_result` tool call. Verifies that a new git commit
  exists with prefix `ingest:`. Updates the row to `done` with the commit
  SHA, summary, and pages-changed lists.
- On any failure (timeout, missing report_result, no commit, exception):
  bump retry_count, schedule retry. After 3 retries set `failed` and notify.

### Scheduler

- APScheduler with a SQLAlchemy job store backed by the same SQLite database.
- Two cron jobs in MVP:
  - `daily_lint` — at 02:00 local time, inserts a synthetic `resources` row
    with `resource_type='_lint'` and `status='approved'` so the ingest worker
    picks it up and dispatches to the lint prompt instead of the ingest prompt.
  - `weekly_synthesis` — at 09:00 every Sunday, inserts a synthetic row with
    `resource_type='_synthesis_weekly'`.
- Also runs a `retry_sweeper` every 5 minutes that looks for resources stuck
  in non-terminal states (`parsing`, `gating`, `ingesting`) for more than 30
  minutes (likely the worker crashed mid-flight) and resets them to their
  previous status with `retry_count + 1`.

### Notifier

In MVP, the resource worker and ingest worker each have a small inline
"notify" function that uses the Telegram bot token to call
`sendMessage` with `reply_to_message_id`. There is **no separate notifier
process** in v1 — keeping it inline avoids another moving part. The function
is idempotent on the resources table: a `notification_sent_at` column is set
within the same transaction that updates `status` to terminal.

## Data flow walkthrough — happy path for a web URL

1. User pastes `https://example.com/great-article` into Telegram.
2. Bot detects URL, type=`web`. Inserts row `r1` with status=`received`,
   `source_url=https://example.com/great-article`. Replies "📥 queued (id=r1)".
3. Resource worker picks up `r1`, sets status=`parsing`. Calls trafilatura,
   gets cleaned markdown. Writes `raw/parsed/web/r1-great-article.md`. Sets
   status=`parsed`, fills `parsed_text_path` and `content_title`.
4. Same worker, next poll: picks up `r1` again (now `parsed`). Sets
   status=`gating`. Calls DeepSeek with first 2000 tokens of parsed text +
   `purpose.md`. Score=82, topics=`["llm-agents", "evals"]`. Sets
   status=`approved`.
5. Ingest worker picks up `r1`. Sets status=`ingesting`. Spawns
   `docker run` for Claude Agent SDK with the parsed file path and the
   ingest prompt. Claude reads CLAUDE.md, index.md, related entity pages;
   creates `wiki/sources/r1-great-article.md`, updates
   `wiki/entities/Andrej-Karpathy.md`, appends to `log.md`. Calls
   `report_result(status="success", pages_created=[...], ...)` and exits.
6. Worker reads the tool-use payload, runs `git rev-parse HEAD` in the wiki
   to confirm the commit, sets status=`done`, fills
   `ingest_commit_sha` and `ingest_summary`. Sends Telegram reply: "✅
   ingested. +2 pages, 1 update. Topics: llm-agents, evals."

## Failure-path examples

- **Bad URL (404)**: trafilatura returns empty. Resource worker writes
  `error_message="empty content"`, sets status=`failed`, notifies. No
  retries (parsing failures are usually permanent — see `12-error-handling.md`).
- **DeepSeek 503**: gate worker catches exception, bumps `retry_count`,
  schedules retry in 60s × 2^n. After 3 fails, marks resource as `approved`
  by **default-accept** policy with `quality_gate_skipped=true`. This is
  intentional: it is better to ingest unsure than to lose the resource.
- **Claude Agent SDK timeout (>10 min)**: ingest worker kills the container,
  status reverts to `approved`, retry scheduled in 1 hour. After 3 fails,
  status=`failed`, notification.
- **`report_result` not called**: same as timeout. The agent did not finish
  cleanly; do not trust any partial commits — the ingest worker checks
  `git status` and if dirty, runs `git reset --hard` before retrying.

## Concurrency and ordering

- The bot is async and handles many simultaneous Telegram updates. SQLite
  inserts are serialized by SQLite's lock; this is fine at the expected
  volume (well under 1 message/sec).
- The resource worker processes one resource at a time. Future versions can
  parallelize by `resource_type` (parsing has different bottlenecks per
  type), but MVP keeps it serial.
- The ingest worker processes **strictly one resource at a time** to avoid
  git races. This is the bottleneck of the system. At expected volumes
  (10–50 resources/day), serial ingest is fine — each takes 1–5 minutes.

## Why no Redis/Celery in MVP

- One owner, expected volume <100 resources/day.
- SQLite polling at 2s intervals is well within SQLite's capability and
  imposes effectively zero load.
- No need for distributed workers, work stealing, or pub/sub.
- Adding Redis would mean another container, another set of failure modes,
  and more code. Deferred until needed.
