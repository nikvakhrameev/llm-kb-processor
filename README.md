# llm-kb

A personal, append-only, **LLM-curated knowledge base**. You feed raw sources
to a Telegram bot; the system parses them, filters out low-value content, and
integrates what survives into a structured, citation-backed Markdown wiki under
git version control.

Inspired by Andrej Karpathy's "LLM Wiki" idea: the wiki is a **compounding
artifact** that the LLM writes and you read. Each new source mutates the wiki —
entity pages get updated, concept pages gain claims, contradictions are flagged,
syntheses are revised. Over time it becomes a hand-curated "second brain" of
what you've actually consumed and how the pieces fit together.

This is **not** a RAG store and **not** a chatbot. The bot ingests and reports;
it does not (yet) answer questions about the wiki.

## How it works

```
Telegram user → Bot (aiogram) → raw/inbox/ + SQLite (status=received)
                                     │
                                     ▼
                              Resource Worker → parsers → DeepSeek quality gate → approved | rejected
                                     │
                                     ▼
                              Ingest Worker → claude-agent-sdk → wiki mutations + git commit → done
                                     │
                                     ▼
                              Scheduler → cron (lint, synthesis) + stuck-row sweeper
```

Four long-lived Python processes share **one SQLite database** (WAL mode).
Concurrency is handled by WAL + compare-and-swap status transitions; at most one
ingest runs at a time (process-wide asyncio lock) to avoid git races.

### Pipeline state machine

```
received → parsing → parsed → gating → approved → ingesting → done
                                              ↘ rejected (terminal)
                                              ↘ failed   (terminal)
```

Stuck rows are reset by the sweeper after 30 min. Retries: 3 attempts with
exponential backoff (60s → 120s → 240s). After 3 infrastructure failures the
quality gate default-accepts (score 65) so sources aren't lost to flaky APIs.

## Supported inputs

| Type    | Trigger                                              |
|---------|------------------------------------------------------|
| web     | Message contains a URL that is not a YouTube link    |
| youtube | Message contains a `youtube.com` / `youtu.be` link   |
| pdf     | Document attachment, mime `application/pdf`           |
| md      | Document attachment, extension `.md` / `.markdown`   |
| text    | Plain text message with no URL                       |
| voice   | Voice message                                        |

Anything else (photos, videos, stickers, other documents) is rejected with a
"not supported" reply.

## Two repos

The application code (this repo) is separate from the knowledge base content,
which lives in its own git repo at `../llm-kb-wiki/`. The ingest agent only ever
commits **locally** to the wiki repo; optional autopush is controlled by
`KB_GIT_AUTOPUSH`.

Wiki layout (created by `scripts/init-wiki.sh`):

```
wiki/entities/          One page per person/company/product/tool (Title-Case.md)
wiki/concepts/          One page per idea/method/theory (lowercase-kebab.md)
wiki/sources/           One page per ingested source
wiki/syntheses/weekly/  Weekly synthesis pages (YYYY-Www.md)
wiki/syntheses/lint/    Daily lint digests (YYYY-MM-DD.md)
raw/inbox/              Original attachments (read-only)
raw/parsed/             Parsed sources with YAML frontmatter (read-only)
purpose.md              Owner-only scope/tone definition
CLAUDE.md               The ingest agent's playbook (schemas, rules, workflows)
index.md, log.md        Auto-maintained index + append-only event log
```

## Tech stack

- **Python 3.12**, venv at `.venv/`, flat `app/` namespace (no `src/`)
- **aiogram** — Telegram bot
- **DeepSeek** (via `openai` SDK) — quality gate
- **claude-agent-sdk** — ingest agent, pointed at DeepSeek via
  `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`
- **trafilatura** (web), **pymupdf4llm** (pdf), **youtube-transcript-api** /
  **yt-dlp** (youtube) — parsers
- **APScheduler** — cron jobs
- Raw `sqlite3` with WAL, no ORM; writes wrapped in `asyncio.to_thread()`
- **pydantic-settings** singleton loaded from `.env`
- `uuid4` resource IDs

## Setup

```bash
# 1. Python env + deps
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Config
cp .env.example .env        # then fill in API keys + paths

# 3. Initialize the wiki repo (default: ../llm-kb-wiki)
./scripts/init-wiki.sh

# 4. Apply the DB schema
sqlite3 "$STATE_DB" < migrations/0001_init.sql
```

Required `.env` values: `DEEPSEEK_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`, `OWNER_CHAT_ID`, `KB_ROOT`,
`STATE_DB`. See `.env.example` for the full list (tuning, cron schedule, git
remote/SSH key).

## Running

Each process runs independently:

| Command                          | Process         |
|----------------------------------|-----------------|
| `python -m app.bot`              | Telegram bot    |
| `python -m app.worker_resources` | Resource worker |
| `python -m app.worker_ingest`    | Ingest worker   |
| `python -m app.scheduler`        | Cron + sweeper  |

### Docker

`docker-compose.yml` runs all processes from one image, sharing `.env.docker`
and two bind mounts — a host dir holding `state.db` (`/data`) and the wiki repo
(`/wiki`). Set `IMAGE`, `STATE_DB_DIR`, `WIKI_DIR` in `.env`, then:

```bash
docker compose pull
docker compose up -d
```

## Testing

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

Tests use in-memory SQLite + fixture files. No external APIs are called.

## Project layout

```
app/
  parsers/        6 parsers (web, pdf, youtube, md, text, voice)
  workers/        Resource worker + ingest worker
  handlers/       Telegram bot handlers
  cron/           Sweeper, lint, synthesis jobs
  prompts.py      All LLM prompts as constants + render_*() functions
  settings.py     pydantic-settings singleton
  db.py           Raw sqlite3 helpers (WAL, CAS transitions)
migrations/       Raw SQL (0001_init.sql)
scripts/          init-wiki.sh, vps-init.sh
docs/initial/     Design spec (13 files)
tests/            Flat, one test file per module
```

See `docs/initial/` and `technical_spec.md` for the full design.
```

## Non-goals

Not a chat assistant · not multi-user (one allowlisted owner per deployment) ·
not real-time (ingestion is async) · not a media archive (text only in v1).