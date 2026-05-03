# CLAUDE.md — llm-kb Project Overview

Personal, append-only, LLM-curated knowledge base. Raw sources arrive via
Telegram; the system parses, filters, and integrates them into a structured
Markdown wiki under git version control.

## Architecture

Four long-lived Python processes sharing one SQLite database:

```
Telegram user → Bot (aiogram) → raw/inbox/ + SQLite (status=received)
                                     ↓
                              Resource Worker → parsers → quality gate → approved
                                     ↓
                              Ingest Worker → claude-agent-sdk → wiki mutations + git commit → done
                                     ↓
                              Scheduler → cron (lint, synthesis) + sweeper
```

Concurrency model: SQLite WAL + CAS-guarded status transitions. At most one
ingest runs at a time (process-wide asyncio lock) to avoid git races.

## Key conventions

- **Python 3.12**, venv at `.venv/`, dependencies in `pyproject.toml`
- **`app/` flat namespace** — no `src/` layout
- **Knowledge base lives in a separate repo** at `../llm-kb-wiki/`
- **DeepSeek for quality gate** (via `openai` SDK), **claude-agent-sdk for ingest**
  (pointed at DeepSeek via `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` env vars)
- **All LLM prompts** live in `app/prompts.py` as string constants with `render_*()` functions
- **Settings** via `pydantic-settings`, singleton in `app/settings.py`, loaded from `.env`
- **No ORM** — raw `sqlite3` with WAL mode, writes in `asyncio.to_thread()`
- **uuid4** for resource IDs (uuid7 not in Python 3.12 stdlib)

## Entry points

| Command                            | Process           |
|------------------------------------|-------------------|
| `python -m app.worker_resources`   | Resource worker   |
| `python -m app.worker_ingest`      | Ingest worker     |
| `python -m app.scheduler`          | Cron + sweeper    |
| `python -m app.bot`                | Telegram bot      |

All need `.env` with API keys. See `.env.example`.

## Directory map

```
app/                  # Application code
  parsers/            #  6 parsers (web, pdf, youtube, md, text, voice)
  workers/            #  Resource worker + ingest worker
  handlers/           #  Telegram bot handlers
  cron/               #  Sweeper, lint, synthesis jobs
  ingest/             #  (reserved for future ingest sub-modules)
tests/                # Flat, one test file per module
migrations/           # Raw SQL (0001_init.sql)
scripts/init-wiki.sh  # Creates ../llm-kb-wiki/ repo
docs/initial/         # Design docs (13 files — the spec)
```

## State machine

`received → parsing → parsed → gating → approved → ingesting → done`
                                                  ↘ rejected (terminal)
                                                  ↘ failed (terminal)

Transitions use CAS guard (`WHERE status=?`). Sweeper resets stuck rows
after 30 min. Retries: 3 attempts, exponential backoff (60s, 120s, 240s).
Quality gate uses default-accept (score=65) after 3 infrastructure failures.

## Testing

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

Tests use in-memory SQLite + fixture files. No external APIs called.
