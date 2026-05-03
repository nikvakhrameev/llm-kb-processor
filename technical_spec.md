# Technical Specification — llm-kb

## 1. System Overview

**llm-kb** is a personal, append-only, LLM-curated knowledge base. Raw sources
arrive via Telegram; the system parses, filters, and integrates them into a
structured Markdown wiki under git version control. Periodic background jobs
lint and synthesize the wiki to keep it coherent.

### 1.1 Design principles

- **Append-only**: sources are immutable once ingested. Superseded sources are
  marked, never deleted.
- **Citation-backed**: every claim in the wiki cites a source via wikilink.
- **Crash-safe**: workers can be killed at any time without leaving the system
  in an inconsistent state. SQLite WAL + CAS-guarded state transitions
  guarantee recovery.
- **LLM-curated, not RAG**: each new source mutates the wiki — entity pages
  are updated, concept pages get new claims, contradictions are flagged,
  syntheses are revised.
- **One owner**: single-user system, allowlisted by Telegram user ID.

### 1.2 Non-goals (v1)

- Not a chat assistant. The bot ingests and reports; it does not answer questions.
- Not multi-user. One owner per deployment.
- Not real-time. Ingestion is asynchronous.
- Not a media archive. Text only; images and figures are ignored.

### 1.3 Supported input types

| Type     | Trigger                                             |
|----------|-----------------------------------------------------|
| web      | Message contains a single non-YouTube URL           |
| youtube  | Message contains a youtube.com or youtu.be link     |
| pdf      | Document attachment with MIME `application/pdf`     |
| md       | Document attachment with extension `.md` or `.markdown` |
| text     | Plain text message ≥ 50 characters, no URLs         |
| voice    | Voice message (`message.voice`)                     |

Everything else (photos, videos, stickers, other document types, audio files
that are not voice messages) is rejected immediately with an explanation.

---

## 2. Architecture

### 2.1 Process topology

Four long-lived Python processes share one SQLite database:

```
Telegram user → [Bot]  detect type → save raw → insert SQLite row (status=received)
                     │                                            │
                     │  immediate ack reply                       │
                     ▼                                            ▼
"queued for processing"                                  [Resource Worker]
                                                          parses input → raw/parsed/...
                                                          DeepSeek quality gate
                                                          status=approved | rejected
                                                                  │
                                                                  ▼
                                                          [Ingest Worker]
                                                          claude-agent-sdk
                                                          mutates wiki, commits to git
                                                          calls report_result()
                                                                  │
                                                                  ▼
                                                          [Notifier]
                                                          replies with summary
```

In parallel, two cron jobs run against the wiki:

- **Daily lint** (02:00 local) — scans for contradictions, orphans, stale
  claims, dangling links. Sends a digest to the owner.
- **Weekly synthesis** (Sunday 09:00 local) — reads the past week of `log.md`
  and generates a synthesis page summarizing what was learned.

### 2.2 Component contracts

#### Bot
- Long-polls Telegram updates via aiogram 3.x.
- Allowlist check: silent drop for non-allowed user IDs.
- Type detection → attachment download (if needed) → SQLite insert → ack reply.
- Does **not** call any LLM or parse content. Pure intake.

#### Resource Worker
- Single asyncio poll loop. Polls SQLite for `status IN ('received', 'parsed')`.
- `received` → parser dispatch → write `raw/parsed/<type>/<slug>.md` → `parsed`.
- `parsed` → DeepSeek quality gate → `approved` or `rejected`.
- Handles retries with exponential backoff, max 3 attempts per stage.
- Notifies on terminal states (`done`, `failed`, `rejected`).

#### Ingest Worker
- Single asyncio poll loop with process-wide lock (one ingest at a time).
- Polls SQLite for `status = 'approved'`.
- Invokes `claude-agent-sdk` against the parsed text.
- Agent reads wiki, creates/updates pages, git commits.
- Captures `report_result` MCP tool call, verifies commit prefix.
- On failure: rollback git, schedule retry.

#### Scheduler
- APScheduler with SQLAlchemy job store on the same SQLite.
- Three jobs: daily lint, weekly synthesis, stuck-row sweeper (every 5 min).
- Lint and synthesis enqueue synthetic `resources` rows (`_lint`, `_synthesis_weekly`)
  that the ingest worker picks up with different prompts.

#### Notifier
- Called inline by workers when transitioning to terminal states.
- Idempotent: checks `notification_sent_at` before sending.
- Logs to stdout. Sends Telegram message when bot token is configured.

### 2.3 Concurrency model

- SQLite in WAL mode. Each process opens its own connection.
- Status transitions use CAS guard (`WHERE status = ?`) — only one worker can
  transition a row from a given state.
- Resource worker processes one resource per tick, sequential.
- Ingest worker holds `asyncio.Lock` — strictly one ingest at a time to avoid
  git races.
- Sweeper runs every 5 minutes, resets rows stuck in in-flight states for >30 min.

---

## 3. Data Model

### 3.1 SQLite schema (`state.db`)

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE resources (
    id                       TEXT PRIMARY KEY,        -- UUIDv4
    resource_type            TEXT NOT NULL,           -- web|pdf|md|youtube|text|voice|_lint|_synthesis_weekly
    status                   TEXT NOT NULL,           -- see §4 State Machine

    -- Telegram metadata (NULL for system rows)
    telegram_chat_id         INTEGER,
    telegram_message_id      INTEGER,
    telegram_user_id         INTEGER,

    -- Resource content references
    source_url               TEXT,
    original_file_path       TEXT,                    -- raw/inbox/... (relative to kb_root)
    parsed_text_path         TEXT,                    -- raw/parsed/... (relative to kb_root)
    inline_text              TEXT,                    -- text type only
    content_title            TEXT,
    content_hash             TEXT,                    -- sha256, optional in MVP

    -- Quality gate
    quality_score            INTEGER,                 -- 0..100
    quality_rationale        TEXT,
    quality_topics           TEXT,                    -- JSON array of strings
    quality_gate_skipped     INTEGER NOT NULL DEFAULT 0,  -- boolean

    -- Ingest
    ingest_commit_sha        TEXT,
    ingest_summary           TEXT,                    -- JSON: {pages_created, pages_updated, warnings}
    ingest_log_entry         TEXT,                    -- markdown bullet for log.md

    -- Pipeline control
    retry_count              INTEGER NOT NULL DEFAULT 0,
    next_attempt_at          TEXT,                    -- ISO-8601, NULL = ready now
    error_message            TEXT,

    -- Notification
    notification_sent_at     TEXT,

    -- Timestamps
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at             TEXT
);

CREATE INDEX idx_resources_status_attempt
    ON resources (status, next_attempt_at, created_at);

CREATE INDEX idx_resources_telegram
    ON resources (telegram_chat_id, telegram_message_id);

CREATE UNIQUE INDEX idx_resources_tg_unique
    ON resources (telegram_chat_id, telegram_message_id)
    WHERE telegram_message_id IS NOT NULL;
```

#### Events table (audit log)

```sql
CREATE TABLE events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id   TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    event_type    TEXT NOT NULL,           -- status_change|parser_error|gate_result|ingest_result|notify_sent|retry_scheduled|sweeper_reset
    payload       TEXT,                    -- JSON
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_events_resource ON events (resource_id, created_at);
```

### 3.2 Filesystem layout

```
kb_root/                        # Separate git repo (llm-kb-wiki)
├── CLAUDE.md                   # Agent playbook (loaded by claude-agent-sdk)
├── purpose.md                  # Owner's interests and scope
├── index.md                    # Auto-maintained listing of all wiki pages
├── log.md                      # Append-only chronological event log
├── raw/
│   ├── inbox/                  # Original attachments as received
│   ├── parsed/
│   │   ├── web/
│   │   ├── pdf/
│   │   ├── youtube/
│   │   ├── text/
│   │   └── voice/
│   └── rejected/               # Quality-gated out, kept for audit
└── wiki/
    ├── entities/               # One page per person, company, product, tool
    ├── concepts/               # One page per idea, method, theory
    ├── sources/                # One page per ingested source
    └── syntheses/
        ├── weekly/             # YYYY-WW.md
        ├── lint/               # Daily lint digests (YYYY-MM-DD.md)
        └── topics/             # Ad-hoc topic syntheses
```

### 3.3 File naming conventions

- **Parsed files**: `<first8-of-uuid>-<kebab-title>.md`, truncated to 60 chars
- **Entity pages**: Title Case with hyphens — `Andrej-Karpathy.md`
- **Concept pages**: lowercase kebab-case — `mixture-of-experts.md`
- **Source pages**: `<first8>-<kebab-title>.md` — `0193f7a8-great-article.md`
- **Syntheses**: `YYYY-Www.md` (weekly), `YYYY-MM-DD.md` (lint)

### 3.4 Git conventions

| Prefix       | Author          | Trigger                              |
|--------------|-----------------|--------------------------------------|
| `ingest:`    | Claude agent    | Successful source ingestion          |
| `lint:`      | Claude agent    | Daily lint cleanup                   |
| `synthesis:` | Claude agent    | Weekly or topic synthesis            |
| `manual:`    | Human           | Hand edits or auto-snapshots         |

The ingest worker verifies that the latest commit after an agent run starts
with the expected prefix. A commit not matching indicates the agent did
something off-script and is treated as a failure.

---

## 4. State Machine

### 4.1 States

| State       | Set by            | Meaning                                              |
|-------------|-------------------|------------------------------------------------------|
| `received`  | bot               | Row inserted, raw input saved                        |
| `parsing`   | resource worker   | Worker is currently parsing this row                 |
| `parsed`    | resource worker   | Parsed text is on disk, ready for quality gate       |
| `gating`    | resource worker   | Worker is calling DeepSeek quality gate              |
| `approved`  | resource worker   | Gate passed (or skipped after retries) — ready for ingest |
| `rejected`  | resource worker   | Gate scored below threshold (terminal)               |
| `ingesting` | ingest worker     | Claude Agent SDK is running                          |
| `done`      | ingest worker     | Wiki updated and committed (terminal)                |
| `failed`    | any worker        | All retries exhausted (terminal)                     |

### 4.2 Transition graph

```
                   ┌─────────────┐
                   │  received   │  (bot)
                   └──────┬──────┘
                          │
                          ▼
                   ┌─────────────┐
                   │  parsing    │
                   └──────┬──────┘
                parse OK  │  parse error → retry → failed
                          ▼
                   ┌─────────────┐
                   │  parsed     │
                   └──────┬──────┘
                          │
                          ▼
                   ┌─────────────┐
                   │  gating     │
                   └──────┬──────┘
            ┌─────────────┼──────────────┐
     score<60       60≤score≤100   3 errors
            │             │              │
            ▼             ▼              ▼
     ┌──────────┐  ┌──────────┐    skip-and-accept
     │ rejected │  │ approved │←───────┘
     └──────────┘  └─────┬────┘
      (terminal)         │
                         ▼
                  ┌─────────────┐
                  │  ingesting  │
                  └──────┬──────┘
            success │            │ failure → retry → failed
                    ▼            ▼
             ┌──────────┐   ┌──────────┐
             │   done   │   │  failed  │
             └──────────┘   └──────────┘
              (terminal)     (terminal)
```

### 4.3 State transition implementation

All transitions use a CAS guard to prevent races:

```python
def transition(resource_id, from_status, to_status, **fields):
    with db.transaction():
        n = db.execute("""
            UPDATE resources
               SET status = ?, updated_at = datetime('now'), ...
             WHERE id = ? AND status = ?
        """, (to_status, resource_id, from_status))
        assert n == 1, f"race: expected status={from_status}"

        db.execute("""
            INSERT INTO events (resource_id, event_type, payload)
            VALUES (?, 'status_change', ?)
        """, (resource_id, json.dumps({"from": from_status, "to": to_status, ...})))
```

If `from_status` doesn't match (another worker already transitioned it),
`rowcount` is 0 and a `RuntimeError` is raised. The caller catches this and
moves on.

### 4.4 Retry policy

- Max 3 retries per stage.
- Exponential backoff: 60s → 120s → 240s.
- Retry rolls back to the previous **pickable** status (e.g., `parsing` → `received`,
  `ingesting` → `approved`), not staying in the in-flight status. This is
  critical for the sweeper.
- `retry_count` is **never reset** across a resource's lifetime. A resource
  that fails parsing twice has only 1 retry remaining for all subsequent stages.

#### Stage-specific behavior

| Stage     | Terminal error type     | Retry behavior                                      |
|-----------|------------------------|-----------------------------------------------------|
| Parsing   | `ParseError`           | No retry (permanent: 404, corrupt file, empty)      |
| Parsing   | `TransientParseError`  | Retry up to 3x with backoff                         |
| Parsing   | Unknown exception      | Retry (treated as transient)                        |
| Gating    | API error, network     | Retry up to 3x. After exhaustion: **default-accept** |
| Ingesting | SDK error, timeout     | Retry up to 3x. After exhaustion: terminal `failed`  |

#### Quality gate default-accept

The quality gate is the **only** stage that does not fail terminally. After 3
DeepSeek failures, the resource is marked `approved` with `quality_gate_skipped=1`
and a synthetic score of 65. Rationale: losing a source the user explicitly
sent is worse than ingesting one of unknown quality.

### 4.5 Stuck-row sweeper

Runs every 5 minutes via the scheduler:

```sql
UPDATE resources
   SET status = CASE status
                  WHEN 'parsing'   THEN 'received'
                  WHEN 'gating'    THEN 'parsed'
                  WHEN 'ingesting' THEN 'approved'
                END,
       retry_count = retry_count + 1,
       error_message = COALESCE(error_message, '') || ' [sweeper:stuck]'
 WHERE status IN ('parsing', 'gating', 'ingesting')
   AND datetime(updated_at) < datetime('now', '-30 minutes');
```

Workers should periodically update `updated_at` during long operations to
prevent false positives (heartbeat pattern).

### 4.6 Ingest worker lock

The ingest worker holds a process-wide `asyncio.Lock` during ingest so that
concurrent polls don't pick up a second resource while one is running. Combined
with the CAS guard, this ensures at most one ingest at a time across the system.

---

## 5. Pipeline Stages

### 5.1 Intake (Telegram Bot)

**File**: `app/bot.py`, `app/handlers/messages.py`

1. User sends message to Telegram bot.
2. Auth middleware checks `from_user.id` against `ALLOWED_USER_IDS`. Silent drop
   if not allowed.
3. `detect_type()` runs in priority order: voice → document → text with URLs →
   plain text.
4. If unsupported: reply with explanation, do not persist.
5. If supported: download attachment to `raw/inbox/<uuid>.<ext>` (for files/voice),
   or store text/URL inline.
6. Insert `resources` row with `status='received'`.
7. Reply with "queued for processing (id `<short>`)" ack.

#### Type detection logic

```python
def detect_type(message: Message) -> ResourceType | None:
    if message.voice is not None:
        return ResourceType.VOICE
    if message.document is not None:
        # Check mime type and file extension
        if pdf: return ResourceType.PDF
        if md/markdown: return ResourceType.MD
        return None  # unsupported document
    if message.text:
        urls = extract_urls(message.text)
        if len(urls) == 1:
            if is_youtube(url): return ResourceType.YOUTUBE
            return ResourceType.WEB
        if len(urls) > 1: return None  # ambiguous
        if len(text) >= 50: return ResourceType.TEXT
        return None  # too short
    return None  # photo, sticker, video, etc.
```

#### URL extraction

Regex: `https?://[^\s)>\]]+` with trailing punctuation stripped (`,.;:!?`).
Also reads Telegram `MessageEntity` text links and de-duplicates.

#### YouTube detection

Hostnames: `youtube.com`, `www.youtube.com`, `m.youtube.com`, `youtu.be`,
`music.youtube.com`.

#### Attachment download

- Telegram `getFile` API, max 20 MB.
- Saved to `kb_root/raw/inbox/<resource_id>.<ext>`.
- Path stored as relative to `kb_root` in `original_file_path`.
- Voice messages always `.ogg`.

### 5.2 Parsing

**File**: `app/parsers/`, `app/workers/resources.py`

The resource worker picks up `received` rows and dispatches to the appropriate
parser based on `resource_type`.

#### Parser contract

```python
async def parse_<type>(resource: Resource, kb_root: Path) -> ParseResult: ...
```

- **Input**: `Resource` dataclass + `kb_root` path.
- **Output**: `ParseResult(parsed_path, title, char_count, parser_id, extra)`.
- **Errors**: `ParseError` (terminal, no retry) or `TransientParseError` (retriable).
- **Side effect**: writes markdown with YAML frontmatter to `raw/parsed/<type>/<slug>.md`.

All sync libraries wrapped in `asyncio.to_thread()`.

#### Parsers

| Parser    | Library                      | Notes |
|-----------|------------------------------|-------|
| **web**   | `trafilatura`                | `favor_precision=True`, `include_images=False`, min 200 chars. Title from metadata or fallback to URL. |
| **youtube**| `youtube-transcript-api`    | Tries `en`, `ru` languages. Falls back to any available. Groups segments into ~30s timestamped paragraphs (`[HH:MM:SS] text`). Metadata via `yt-dlp -J`. |
| **pdf**   | `pymupdf4llm`                | Converts to markdown with headings, tables, lists. Min 200 chars. Title from PDF metadata via `fitz`. No OCR in MVP. |
| **md**    | passthrough                  | Reads file, extracts first `# heading` as title. Min 50 chars. |
| **text**  | inline                       | Uses `resource.inline_text` directly. First line as title (truncated to 80 chars). Min 50 chars. |
| **voice** | `faster-whisper`             | "small" model, CPU, int8 quantization. Lazy singleton (load once). `vad_filter=True`. Min 30 chars. Title from first 60 chars of transcript. |

#### Parsed file format

Every parsed file has YAML frontmatter:

```yaml
---
resource_id: <uuid>
resource_type: web|pdf|youtube|text|voice|md
source_url: <url or null>
title: "<title>"
fetched_at: <ISO-8601>
char_count: <int>
parser: "<parser_id>"
---
# Title

(clean markdown body)
```

### 5.3 Quality Gate

**File**: `app/quality_gate.py`, `app/prompts.py`

#### Provider

DeepSeek via `openai.AsyncOpenAI` with `base_url=https://api.deepseek.com/v1`.
Model: `deepseek-chat` (configurable).

#### Input

1. Full text of `purpose.md` from the wiki (system message, cached server-side).
2. First ~2000 tokens of parsed body (user message, truncated, not summarized).
3. Metadata: resource type, source URL/filename, title.

#### Scoring rubric

- **0–30**: garbage — parsing artifacts, error pages, paywalls, noise, music captions.
- **30–60**: marginal — real content but very short, off-topic, or duplicative.
- **60–80**: useful — coherent content with substance.
- **80–100**: highly useful — clearly aligned with stated interests.

Default is permissive. When unsure, score 65. The bar is "is this actually
content?", not "is this important?".

#### Output

```python
@dataclass
class GateResult:
    score: int                 # 0..100, clamped
    rationale: str             # 1-2 sentences
    topics: list[str]          # ≤6 lowercase kebab-case tags
    raw_response: str          # full JSON for audit
```

Response format: `{"type": "json_object"}` with explicit schema in the prompt.
Temperature: 0.0. Max tokens: 400.

#### Threshold

`GATE_ACCEPT_THRESHOLD = 60` (configurable via env).

- `score >= 60` → `approved`, gate result stored in `quality_score`,
  `quality_rationale`, `quality_topics`.
- `score < 60` → `rejected`, parsed file moved to `raw/rejected/`, Telegram
  notification sent with score and rationale.

#### Default-accept on infrastructure failure

```python
async def gate_with_retries(...) -> tuple[GateResult, bool]:
    for attempt in range(3):
        try:
            return await quality_gate(...), False
        except Exception as e:
            await asyncio.sleep(60 * (2 ** attempt))
    # All retries failed — default-accept
    return GateResult(score=65, rationale=f"gate-skipped: {e}",
                      topics=[]), True
```

`quality_gate_skipped=1` is set so this is visible in audit.

#### Cost

~3,500 input tokens + ~150 output tokens per call. At DeepSeek pricing with
prompt caching, roughly $0.0001 per call. At 50 resources/day: ~$0.15/month.

### 5.4 Ingest (Wiki Mutation)

**File**: `app/workers/ingest.py`

The ingest worker picks up `approved` rows and invokes `claude-agent-sdk` to
mutate the wiki.

#### SDK integration

`claude-agent-sdk` is pointed at DeepSeek via environment variables:

```python
options = ClaudeAgentOptions(
    cwd=str(repo),
    system_prompt={"type": "preset", "preset": "claude_code"},
    setting_sources=["project"],       # auto-loads CLAUDE.md from cwd
    permission_mode="acceptEdits",     # auto-approve Edit/Write within cwd
    allowed_tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash",
                   "mcp__kb_ingest__report_result"],
    mcp_servers={"kb_ingest": kb_ingest_mcp},
    max_turns=max_turns,
    model=model,
    hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[bash_pre_hook])]},
    env={
        "ANTHROPIC_BASE_URL": settings.anthropic_base_url,
        "ANTHROPIC_AUTH_TOKEN": settings.anthropic_auth_token,
    },
)
```

#### MCP tool: `report_result`

The agent **must** call `report_result` exactly once at the end of its run.
This is the structured contract between agent and worker.

```python
@tool("report_result", "Report final result of ingest...", {
    "status": str,          # "success" | "partial" | "failed"
    "pages_created": list,  # paths relative to kb_root
    "pages_updated": list,
    "log_entry": str,       # one-line markdown for log.md
    "summary": str,         # 1-2 sentences for Telegram reply
    "warnings": list,       # e.g., contradictions found
})
```

The worker captures the tool call from the async message stream, not the
return value.

#### Bash hook

Only git-safe commands are permitted:

```
git status, git diff, git add, git commit, git log, git rev-parse
ls, cat, wc
```

All other Bash commands are denied via `PreToolUse` hook returning
`permissionDecision: deny`. This blocks `rm`, `find`, `curl`, `git push`,
`git reset`, etc.

#### Git workflow

1. **Pre-flight**: `ensure_clean()` — if the working tree is dirty, auto-commit
   as `manual: pre-ingest snapshot` to preserve any manual edits.
2. **Run agent**: stream messages from `query(prompt, options)`.
3. **Capture result**: find `mcp__kb_ingest__report_result` in `ToolUseBlock`
   messages. Also capture `ResultMessage` for cost/duration.
4. **Verify**: HEAD commit must have expected prefix (`ingest:`, `lint:`, or
   `synthesis:`). If not, treat as failure.
5. **Success**: transition to `done`, store commit SHA and summary, notify.
6. **Failure**: `rollback()` — `git reset --hard` + `git clean -fd` to the
   pre-ingest SHA. Schedule retry.

#### Dispatch by resource type

| Resource type           | Prompt function        | Commit prefix    | Max turns |
|-------------------------|------------------------|------------------|-----------|
| web, pdf, youtube, etc. | `render_ingest()`      | `ingest:`        | 25        |
| `_lint`                 | `render_lint()`        | `lint:`          | 40        |
| `_synthesis_weekly`     | `render_synthesis()`   | `synthesis:`     | 60        |

#### Timeout and retry

Hard timeout of 600 seconds (configurable). On timeout: cancel SDK iterator,
rollback git, schedule retry. After 3 retries: terminal `failed`, notify.

#### Concurrency

Process-wide `asyncio.Lock` — strictly one ingest at a time to avoid git
races on the working tree.

---

## 6. Wiki Conventions

### 6.1 Top-level files

| File          | Owner        | Purpose |
|---------------|-------------|---------|
| `CLAUDE.md`   | Maintainer   | Agent playbook: mission, wiki structure, page schemas, naming, ingest/lint/synthesis workflows, forbidden actions |
| `purpose.md`  | Owner        | What belongs in this wiki, interests, scope, depth, tone |
| `index.md`    | Agent (auto) | Flat list of all wiki pages grouped by category |
| `log.md`      | Agent (auto) | Append-only chronological event log |

### 6.2 Page schemas

#### Source page (`wiki/sources/<slug>.md`)

```markdown
---
resource_id: <uuid>
resource_type: web|pdf|youtube|text|voice|md
source_url: <url or null>
title: "<title>"
ingested_at: <ISO-8601>
parsed_path: raw/parsed/<type>/<slug>.md
quality_score: <0-100>
topics: [<list>]
---

# <Title>

## TL;DR
One short paragraph. The most compressed possible takeaway.

## Key claims
- Claim 1. [[parsed#para1]]
- Claim 2. [[parsed#para3]]

## Notable details
Optional.

## Connections
- Mentions [[entities/Name]].
- Builds on [[concepts/name]].
- Related to [[sources/other]].
```

#### Entity page (`wiki/entities/<Name>.md`)

```markdown
---
type: entity
created: <ISO-date>
last_updated: <ISO-date>
sources: [<short_ids>]
---

# <Name>

## Overview
One paragraph.

## Roles and affiliations
- Fact with citation [[sources/slug]].

<!-- llm:auto-section -->
## Connections
- Frequently cited alongside [[entities/Other]].
```

#### Concept page (`wiki/concepts/<name>.md`)

```markdown
---
type: concept
created: <ISO-date>
last_updated: <ISO-date>
sources: [<short_ids>]
---

# <Display Name>

## Definition
One paragraph with citation. [[sources/slug]]

## Key claims
- Claim with citation. [[sources/slug]]

## Open questions
- Unresolved?

<!-- llm:auto-section -->
## Connections
```

#### Synthesis page (`wiki/syntheses/weekly/<YYYY-Www>.md`)

```markdown
---
type: synthesis
kind: weekly
week: <YYYY-Www>
source_count: <n>
themes: [<list>]
---

# Weekly Synthesis · <YYYY-Www> · <Theme>

## Theme of the week
...

## <Theme section>
...citations to sources and concept pages...

## Open questions
- ...

## Reading list
- [[sources/slug]] — one-sentence summary.
```

### 6.3 Wikilink syntax

Obsidian-compatible:
- `[[entities/Name]]` — full page
- `[[entities/Name#Section]]` — section anchor
- `[[sources/slug#para3]]` — paragraph anchor (1-indexed, ignores frontmatter)
- `[[concepts/name|alias]]` — aliased display

### 6.4 Forbidden agent actions

- Edit `purpose.md` (owner-only).
- Edit content above `<!-- manual:keep -->` markers.
- Delete a source page (mark `<!-- llm:superseded by [[...]] -->` instead).
- Delete claims with citations (mark them disputed instead).
- Edit anything under `raw/` (read-only).
- Push to any remote.
- Fabricate facts not in the source or already in the wiki.

---

## 7. Prompts

### 7.1 Quality gate system prompt

```
You are a quality filter for a personal knowledge base.

The owner has stated their interests and what belongs in this knowledge base
in the PURPOSE section below. Your job is to decide whether a parsed resource
contains coherent, useful content that is worth integrating into the wiki.

You score on a scale of 0-100:
- 0-30: garbage (parsing artifacts, cookie banners, JS errors, paywalls, noise)
- 30-60: marginal (real content but very short, off-topic, or duplicative)
- 60-80: useful (coherent content with substance)
- 80-100: highly useful (clearly aligned with stated interests)

Default to permissive. When unsure, score 65.

PURPOSE OF THIS KNOWLEDGE BASE:
---
{purpose}
---

Return strict JSON only:
{"score": <0..100>, "rationale": "<1-2 sentences>", "topics": ["<tag>", ...]}
```

### 7.2 Ingest prompt

```
You are integrating a new source into the personal knowledge base.

The wiki conventions, page schemas, and workflow are described in CLAUDE.md
in the working directory. Read it first if you have not already.

The new source is at: {parsed_relpath}

Resource metadata: resource_id, resource_type, title, topics, quality_score.

Your task:
1. Read CLAUDE.md, purpose.md, and index.md.
2. Read the new source file.
3. Identify entities and concepts. Search for existing overlapping wiki pages.
4. Read those overlapping pages.
5. Create wiki/sources/<slug>.md summarizing the source.
6. Update or create affected entity/concept pages with citation.
7. Update index.md with new pages.
8. Append a one-line entry to log.md.
9. git add and git commit with prefix "ingest: <slug>".
10. Call report_result exactly once with your final summary.

Constraints:
- Every claim MUST cite a source page using wikilink syntax.
- Do not invent facts not present in the source or already in the wiki.
- If you find a contradiction, do NOT silently overwrite. Add the new claim
  with citation, mark with a "Contradictions" section, and include in warnings.
- Only edit files under wiki/, log.md, or index.md.
- Do not push to any remote.
```

### 7.3 Lint prompt

```
You are running the daily lint of the personal knowledge base.

Read CLAUDE.md and purpose.md first.

Goals:
1. Find dangling [[wikilinks]] — links to files that do not exist.
2. Find orphan pages — no incoming wikilinks.
3. Find stub pages — bodies under ~10 lines.
4. Find duplicate concepts — multiple pages for same idea.
5. Find direct contradictions across pages.
6. Verify index.md against filesystem.

Output:
1. Create wiki/syntheses/lint/<YYYY-MM-DD>.md with findings by category.
2. Optionally auto-fix: rebuild index.md, comment out dangling links,
   tag flagged pages with <!-- lint:<type> -->.
3. git add and git commit with prefix "lint: <YYYY-MM-DD>".
4. Call report_result.
```

### 7.4 Weekly synthesis prompt

```
You are writing the weekly synthesis page.

Time window: {week_start} through {week_end} (ISO week {iso_week_label}).

Procedure:
1. Read log.md and isolate entries within the window.
2. Read source pages created in the window.
3. Identify themes — clusters of sources discussing related ideas.
4. Read entity/concept pages those sources updated.
5. Look for unexpected connections, open questions, contradictions.

Output:
1. Create wiki/syntheses/weekly/{iso_week_label}.md with:
   - Theme of the week
   - Per-theme synthesis with citations
   - Open questions section
   - Reading list
2. Update relevant entity/concept pages with "Mentioned in syntheses" links.
3. git add and git commit with prefix "synthesis: weekly {iso_week_label}".
4. Call report_result.

Constraints:
- Be honest. If the week was thin, say so. Do not pad.
- Every claim cites a source. Synthesis is not invention.
- Cap at ~1500 words.
```

---

## 8. Scheduler and Cron Jobs

**File**: `app/scheduler.py`, `app/cron/`

### 8.1 Scheduler configuration

APScheduler with SQLAlchemy job store on the same SQLite database. WAL mode
explicitly enabled on the SQLAlchemy engine to avoid lock conflicts with the
worker processes.

### 8.2 Jobs

| Job ID              | Trigger          | Function              | Description |
|---------------------|------------------|-----------------------|-------------|
| `daily_lint`        | 02:00 daily      | `enqueue_lint()`      | Inserts `_lint` row with `status='approved'` |
| `weekly_synthesis`  | Sun 09:00        | `enqueue_synthesis()` | Inserts `_synthesis_weekly` row |
| `sweeper`           | Every 5 minutes  | `run_sweeper()`       | Resets rows stuck in in-flight states for >30 min |

All times in local timezone (configurable via `TZ` env var).

### 8.3 Sweeper mechanics

Resets rows where `status IN ('parsing','gating','ingesting')` and
`updated_at < now - 30 minutes`. The 30-minute threshold is generous —
real operations complete in under 10 minutes. If a row is stuck longer,
the worker likely crashed or is wedged.

Rollback mapping:
- `parsing` → `received`
- `gating` → `parsed`
- `ingesting` → `approved`

Each reset increments `retry_count` and appends ` [sweeper:stuck]` to
`error_message` for auditability.

### 8.4 Synthetic resource rows

`_lint` and `_synthesis_weekly` rows reuse the same `resources` table and
ingest worker. They:
- Have `status='approved'` (skip parsing and gating stages).
- Have no `telegram_*` fields (notifications go to `OWNER_CHAT_ID`).
- Are dispatched to different prompts by the ingest worker.
- Use different `max_turns` values (40 for lint, 60 for synthesis).

---

## 9. Notifications

**File**: `app/notifier.py`

### 9.1 Notification triggers

Notifications are sent on terminal state transitions: `done`, `failed`, `rejected`.

### 9.2 Idempotency

`notification_sent_at` is checked before sending and set in the same
transaction as the Telegram API call. If the Telegram call fails, the
field stays NULL and the next transition (or sweeper) retries.

### 9.3 Message format

#### Done
```
Ingested
"<title>"
+<n> pages, <m> updates
ID: <short_id>
```

If warnings: appends `⚠ <n> warning(s)` line.

#### Rejected
```
Skipped (low quality, score <n>)
"<title>"
<rationale>
```

#### Failed
```
Ingest failed
"<title>"
<error_message>
ID: <short_id>
```

### 9.4 Delivery

- Messages with `telegram_message_id` are sent as **replies** to the original
  user message (threaded conversation).
- System rows (`_lint`, `_synthesis_weekly`) are sent as **new messages** to
  `OWNER_CHAT_ID`.
- Falls back to stdout when no Telegram bot token is configured.

---

## 10. Configuration

**File**: `app/settings.py`, `.env.example`

### 10.1 Environment variables

All configuration via environment variables loaded by `pydantic-settings`.

#### API keys

| Variable               | Purpose                              |
|------------------------|--------------------------------------|
| `DEEPSEEK_API_KEY`     | Quality gate API key                 |
| `DEEPSEEK_BASE_URL`    | DeepSeek endpoint (default: `https://api.deepseek.com/v1`) |
| `DEEPSEEK_MODEL`       | Model for quality gate (default: `deepseek-chat`) |
| `ANTHROPIC_BASE_URL`   | claude-agent-sdk endpoint (default: `https://api.deepseek.com/v1`) |
| `ANTHROPIC_AUTH_TOKEN` | claude-agent-sdk auth token          |
| `TELEGRAM_BOT_TOKEN`   | Telegram bot token                   |

#### Access control

| Variable          | Purpose                                   |
|-------------------|-------------------------------------------|
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs         |
| `OWNER_CHAT_ID`    | Chat ID for unsolicited system notifications |

#### Paths

| Variable    | Purpose                                      | Default |
|-------------|----------------------------------------------|---------|
| `KB_ROOT`   | Root of the knowledge base git repo          | `../llm-kb-wiki` |
| `STATE_DB`  | Path to SQLite database file                 | `./state.db` |

#### Tuning

| Variable                      | Purpose                              | Default |
|-------------------------------|--------------------------------------|---------|
| `POLL_INTERVAL_SECONDS`        | Worker poll interval                 | 2       |
| `GATE_ACCEPT_THRESHOLD`        | Minimum score for approval           | 60      |
| `INGEST_TIMEOUT_SECONDS`       | Hard timeout for agent run           | 600     |
| `INGEST_MAX_TURNS`             | Max agent turns for ingest           | 25      |
| `LINT_MAX_TURNS`               | Max agent turns for lint             | 40      |
| `SYNTHESIS_MAX_TURNS`          | Max agent turns for synthesis        | 60      |
| `RETRIES_MAX`                  | Max retry attempts per stage         | 3       |
| `RETRY_BACKOFF_BASE_SECONDS`   | Base seconds for exponential backoff | 60      |
| `SWEEPER_STUCK_MINUTES`        | Minutes before a row is considered stuck | 30  |

#### Cron scheduling

| Variable           | Purpose                    | Default      |
|--------------------|----------------------------|--------------|
| `LINT_HOUR`        | Hour for daily lint        | 2            |
| `LINT_MINUTE`      | Minute for daily lint      | 0            |
| `SYNTHESIS_DAY`    | Day for weekly synthesis   | sun          |
| `SYNTHESIS_HOUR`   | Hour for weekly synthesis  | 9            |
| `SYNTHESIS_MINUTE` | Minute for weekly synthesis| 0            |
| `TZ`               | Timezone for cron jobs     | Europe/Berlin|

---

## 11. Error Handling

### 11.1 Principles

1. **Never lose a resource silently.** Every resource ends in a terminal state
   (`done`, `failed`, `rejected`) and every terminal transition triggers a
   notification.
2. **Crash-safe by default.** SQLite WAL + CAS-guarded transitions + sweeper
   guarantee recovery from worker crashes.
3. **Conservative retries.** Three attempts per stage, exponential backoff.
   Gate uses default-accept, not rejection, on infrastructure failure.
4. **Wiki transactionality.** Either an ingest commits cleanly or the wiki is
   restored to its previous SHA. No half-applied changes.

### 11.2 Stage-by-stage failure matrix

#### Bot (intake)

| Failure                              | Action                                     |
|--------------------------------------|--------------------------------------------|
| Telegram download fails              | Reply error, do not insert SQLite row      |
| Disk write fails                     | Reply error, do not insert SQLite row      |
| SQLite insert fails                  | Reply "system error, try again", log       |
| User not in allowlist                | Silent drop                                |
| Unsupported message type             | Reply with supported-types message         |

#### Resource worker — parsing

| Failure                              | Type      | Action                                |
|--------------------------------------|-----------|---------------------------------------|
| `ParseError` (404, empty, corrupt)   | terminal  | status=`failed`, no retry, notify     |
| `TransientParseError` (rate limit)   | transient | retry up to 3x with backoff           |
| Network timeout                      | transient | retry                                 |
| Worker crash mid-parse               | sweeper   | reset to `received` after 30 min      |
| Unknown exception                    | transient | retry; if 3x fails, terminal `failed` |

#### Resource worker — quality gate

| Failure                              | Action                                              |
|--------------------------------------|-----------------------------------------------------|
| DeepSeek 5xx, network, rate limit    | retry up to 3x with backoff                         |
| All 3 retries fail                   | **default-accept**: status=`approved`, `quality_gate_skipped=1` |
| DeepSeek returns invalid JSON        | retry up to 3x; if all fail, default-accept         |

#### Ingest worker

| Failure                              | Action                                              |
|--------------------------------------|-----------------------------------------------------|
| API 5xx, network, rate limit         | retry up to 3x with backoff                         |
| Timeout (>10 min)                    | cancel async iterator, rollback git, retry          |
| `report_result` not called           | rollback git, retry                                 |
| `report_result.status == "failed"`   | rollback git, retry once; then terminal `failed`    |
| Commit prefix wrong                  | rollback git, retry                                 |
| Worker crash mid-ingest              | sweeper resets to `approved` after 30 min           |

### 11.3 Limits

| Limit                         | Default     |
|-------------------------------|-------------|
| Max retries per stage          | 3           |
| Backoff base                   | 60s         |
| Ingest agent timeout           | 600s        |
| Ingest agent max turns         | 25          |
| Lint max turns                 | 40          |
| Synthesis max turns            | 60          |
| Sweeper stuck threshold        | 30 min      |
| Telegram document download     | 20 MB       |
| Quality gate snippet           | 2000 tokens |

---

## 12. Telegram Bot Interface

### 12.1 Commands

| Command              | Response |
|----------------------|----------|
| `/start`, `/help`    | Show supported types, usage instructions, current allowlist status |
| `/status`            | Last 10 resources from this user (id, type, status, age) |
| `/status <short_id>` | Full detail for one resource: status, error if any, commit SHA, link |

### 12.2 Reply messages

| Situation                        | Reply |
|----------------------------------|-------|
| Accepted                         | `queued for processing (id <short>)` |
| Unsupported type                 | `Unsupported message type. I accept: PDF, MD, web URLs, YouTube URLs, text (50+ chars), and voice messages.` |
| Text too short                   | `Text is too short (min 50 chars)` |
| Multiple URLs in one message     | `Send one link per message please` |
| Download failed                  | `Could not download attachment: <reason>` |
| Rejected (later, via notifier)   | `Skipped (low quality, score <n>): <rationale>` |
| Done (later, via notifier)       | `Ingested. +<n> pages, <m> updates.` |
| Failed (later, via notifier)     | `Ingest failed: <error>.` |

All handler replies use `reply_to_message_id` for threaded conversation.

### 12.3 Auth middleware

```python
async def auth_middleware(handler, event, data):
    user = event.from_user
    if user is None or user.id not in settings.allowed_user_ids:
        return  # silent drop
    return await handler(event, data)
```

Silent drop — no error reply to avoid revealing this is a private bot.

---

## 13. Docker Deployment (Phase 7)

### 13.1 Services

Four containers from the same `kb-bot/app` image with different entrypoints:

| Service           | Command                           | Network access          |
|-------------------|-----------------------------------|-------------------------|
| `bot`             | `python -m app.bot`               | Telegram API            |
| `resource-worker` | `python -m app.worker_resources`  | DeepSeek + arbitrary URLs |
| `ingest-worker`   | `python -m app.worker_ingest`     | DeepSeek only (egress proxy) |
| `scheduler`       | `python -m app.scheduler`         | None (SQLite only)      |

### 13.2 Ingest worker isolation

| Setting                       | Why |
|-------------------------------|-----|
| `read_only: true`             | Root filesystem is read-only. Agent writes only to mounted volumes + tmpfs. |
| `cap_drop: [ALL]`             | No Linux capabilities. Cannot mount, ptrace, change time, etc. |
| `no-new-privileges: true`     | setuid binaries cannot escalate. |
| `kb-egress-only` network      | Outbound traffic restricted to DeepSeek API only via tinyproxy. |
| `raw/parsed` mounted `:ro`    | Belt-and-suspenders: source files cannot be tampered with. |
| `user: 1000:1000`             | Non-root inside container. |
| `tmpfs /tmp`                  | Scratch space, wiped on container restart. |

### 13.3 Egress proxy

Tinyproxy sidecar restricting outbound HTTP to `api.deepseek.com:443` only.
The ingest worker routes Anthropic SDK traffic through `HTTPS_PROXY` env var.
This replaces the Anthropic-only restriction from the original design since
we point at DeepSeek.

### 13.4 Host-level tooling

- **Watchdog** (`scripts/watchdog.sh`): checks `docker compose ps` and alerts
  if any service is not running. Also checks for stuck rows older than 1 hour
  (sweeper should have caught them; if not, the sweeper itself is stuck).
- **Backup** (`scripts/backup-db.sh`): daily `sqlite3 .backup` snapshot of
  `state.db` at 03:00. Keep 30 days.
- **Git push**: wiki commits pushed to `KB_GIT_REMOTE` via post-commit hook
  or 5-minute host cron.

---

## 14. Testing

### 14.1 Test structure

```
tests/
├── conftest.py              # In-memory SQLite fixture, tmp_wiki directory
├── test_db.py               # Schema, CRUD, transitions, CAS guard, retry, poll, sweeper
├── test_models.py           # Dataclass construction and defaults
├── test_parsers.py          # Offline fixtures: text, md, web (mocked fetch_url)
├── test_quality_gate.py     # Gate response parsing, clamping, threshold
├── test_pipeline.py         # Full state machine lifecycle, rejected/failed paths
├── test_ingest.py           # Ingest poll, CAS, transitions, retry
├── test_cron.py             # Lint/synthesis enqueue, sweeper
└── test_bot.py              # URL extraction, YouTube detection, type detection
```

### 14.2 Fixtures

- **`db`**: In-memory SQLite with full schema applied. Fast, isolated, no disk I/O.
- **`tmp_wiki`**: Temporary directory with wiki structure for parser output tests.

### 14.3 Mocking strategy

- **Database tests**: Real SQLite (in-memory), no mocking.
- **Parser tests**: Offline fixtures (HTML string, text, markdown). Web parser
  uses `monkeypatch` on `trafilatura.fetch_url` to return fixture HTML.
- **Quality gate tests**: Test response parsing only (pure function, no API call).
- **Pipeline tests**: Real state machine against in-memory SQLite.
- **Ingest tests**: CAS guard and transition logic (no SDK calls).
- **Bot tests**: Pure function tests for `detect_type()`, `extract_urls()`,
  `is_youtube_url()`.

### 14.4 Running tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

---

## 15. Dependencies

### 15.1 Python packages

| Package                  | Version     | Purpose |
|--------------------------|-------------|---------|
| `openai`                 | ≥1.0.0      | DeepSeek client (OpenAI-compatible API) |
| `claude-agent-sdk`       | ≥0.1.0      | Agent SDK for ingest (pointed at DeepSeek via env) |
| `pydantic-settings`      | ≥2.0.0      | Configuration from environment |
| `trafilatura`            | ≥2.0.0      | Web page extraction |
| `pymupdf4llm`            | ≥0.0.1      | PDF to markdown conversion |
| `youtube-transcript-api` | ≥0.6.0      | YouTube transcript extraction |
| `yt-dlp`                 | ≥2024.0.0   | YouTube metadata fetching |
| `faster-whisper`         | ≥1.0.0      | Voice message transcription |
| `apscheduler`            | ≥3.10.0     | Cron job scheduling |
| `sqlalchemy`             | ≥2.0.0      | APScheduler job store backend |
| `aiogram`                | ≥3.0.0      | Telegram bot framework |
| `pyyaml`                 | ≥6.0        | YAML frontmatter generation |
| `aiofiles`               | ≥23.0.0     | Async file operations |

### 15.2 Dev packages

| Package            | Purpose |
|--------------------|---------|
| `pytest`           | Test runner |
| `pytest-asyncio`   | Async test support |
| `ruff`             | Linting and formatting |
| `mypy`             | Type checking |

---

## 16. Key Design Decisions

1. **DeepSeek for quality gate, claude-agent-sdk for ingest.** Quality gate
   is cheap and simple (single JSON response). Ingest is complex and agentic
   (multi-turn, tool use, file mutations). Using `ANTHROPIC_BASE_URL` and
   `ANTHROPIC_AUTH_TOKEN` env vars, the SDK is pointed at DeepSeek's
   API-compatible endpoint.

2. **Single SQLite database for operational state.** SQLite in WAL mode
   handles the expected volume (under 100 resources/day) with zero operational
   overhead. No Redis, no Celery, no separate state store.

3. **Polling, not push.** Workers poll SQLite at 2-second intervals. At
   expected volume this is effectively zero load and avoids the complexity
   of a message queue.

4. **Wikilinks, not a database.** The wiki is plain Markdown with
   Obsidian-compatible wikilinks. It can be browsed in Obsidian, GitHub,
   or any Markdown viewer. Git provides version history, diff, and backup.

5. **Transactional wiki mutations.** The pre-flight clean check + post-flight
   commit verification + rollback on failure guarantee that the wiki is always
   in a consistent state. Either a new commit lands with the expected prefix,
   or the wiki is byte-identical to before the ingest attempt.

6. **Default-accept on gate failure.** Infrastructure failures should not
   cause data loss. The cost of ingesting one low-quality source is much
   lower than the cost of silently dropping a source the user sent.

7. **uuid4 for resource IDs.** Python 3.12 stdlib doesn't have uuid7. At
   the expected scale (well under 10k rows/month), the time-ordering
   property of uuid7 provides no practical benefit.

---

## 17. Future Enhancements (v2)

- **`/override <id>` command**: let the user force-ingest a resource that
  was rejected by the quality gate.
- **`/lint` and `/synthesize <topic>` commands**: on-demand triggers for
  lint and ad-hoc topic synthesis.
- **Webhook mode**: alternative to long-polling for Telegram.
- **OCR for scanned PDFs**: via `ocrmypdf` preprocessing.
- **JS-rendered web pages**: Playwright sidecar for sites that require JS.
- **YouTube whisper fallback**: download audio and transcribe when no
  transcript is available.
- **Content deduplication**: detect near-duplicate sources before ingest.
- **Image support**: extract and reference figures from PDFs and web pages.
- **Multi-model routing**: different models for different source types or
  quality tiers.
- **uuid7 migration**: when Python 3.14+ is available.
