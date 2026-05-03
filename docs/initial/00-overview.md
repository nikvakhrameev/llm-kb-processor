# Personal Knowledge Base — Overview

## Purpose

A personal, append-only, LLM-curated knowledge base inspired by Andrej Karpathy's
"LLM Wiki" idea: the wiki is a **compounding artifact** that the LLM writes and
the user reads. The user feeds in raw sources via Telegram; the system parses,
filters, and integrates them into a structured Markdown wiki under git version
control. Periodic background jobs lint and synthesize the wiki to keep it
coherent and to surface cross-source insights.

The wiki is **not** a RAG store. Each new source mutates the wiki: entity pages
are updated, concept pages get new claims, contradictions are flagged, syntheses
are revised. Over time the wiki becomes a hand-curated "second brain" that
reflects what the user has actually consumed and how those pieces fit together.

## Non-goals

- Not a chat assistant. The bot ingests and reports; it does not answer
  questions about the wiki (that may come later).
- Not multi-user. One owner per deployment, allowlisted by Telegram user id.
- Not real-time. Ingestion is asynchronous; users receive a reply when the
  pipeline completes (seconds for text, minutes for PDFs and long videos).
- Not a media archive. MVP processes text only. Images and figures inside
  sources are ignored in v1.

## Supported inputs

The Telegram bot accepts the following resource types and rejects everything
else with a "not supported" reply:

| Type    | Trigger                                            |
|---------|----------------------------------------------------|
| web     | Message contains a URL that is not a YouTube link  |
| youtube | Message contains a youtube.com or youtu.be link    |
| pdf     | Document attachment with mime type application/pdf |
| md      | Document attachment with extension .md or .markdown |
| text    | Plain text message with no URL                     |
| voice   | Voice message (`message.voice`)                    |

Anything else (photos, videos, stickers, other document types, audio files that
are not voice messages) is rejected immediately.

## High-level flow

```
Telegram user
     │
     ▼
[Bot]  detect type → save raw → insert SQLite row (status=received)
     │                                            │
     │  immediate ack reply                       │
     ▼                                            ▼
"queued for processing"                  [Resource Worker]
                                          parses input → raw/parsed/...
                                          DeepSeek quality gate
                                          status=approved | rejected
                                                      │
                                                      ▼
                                          [Ingest Worker]
                                          Claude Agent SDK in Docker
                                          mutates wiki, commits to git
                                          calls report_result()
                                                      │
                                                      ▼
                                          [Notifier]
                                          replies to original Telegram
                                          message with summary
```

In parallel, two cron jobs run against the wiki:

- **Daily lint** (02:00 local) — scans for contradictions, orphans, stale
  claims, gaps. Sends a digest to the owner.
- **Weekly synthesis** (Sunday 09:00 local) — reads the past week of `log.md`
  and generates a synthesis page summarizing what was learned and which
  connections emerged.

## Components

| Component         | Runtime          | Responsibility                                 |
|-------------------|------------------|------------------------------------------------|
| `bot`             | Python, aiogram  | Telegram entrypoint, type detection, persistence |
| `resource-worker` | Python, asyncio  | Polls SQLite, parses sources, runs quality gate |
| `ingest-worker`   | Python, asyncio  | Polls SQLite, runs Claude Agent SDK in isolated container, commits wiki changes |
| `scheduler`       | Python, APScheduler | Triggers cron jobs (lint, synthesis) and retries |
| `notifier`        | Python, asyncio  | Sends Telegram replies for completed/failed work |
| `knowledge_base/` | Filesystem + git | Source of truth for wiki content               |
| `state.db`        | SQLite           | Operational state, lifecycle, audit events     |

The notifier is logically separate but in MVP runs in-process inside the
ingest-worker and resource-worker — see `01-architecture.md`.

## Stack

- **Language**: Python 3.12
- **Telegram client**: `aiogram` 3.x
- **Database**: SQLite (single file, WAL mode)
- **Quality gate LLM**: DeepSeek V4 Flash via OpenAI-compatible client
- **Ingest LLM**: Claude (via `claude-agent-sdk` Python package)
- **Parsing**:
  - `trafilatura` — web pages
  - `pymupdf4llm` — PDFs
  - `youtube-transcript-api` + `yt-dlp` — YouTube
  - `faster-whisper` (CPU, small model) — voice messages
- **Scheduling**: `APScheduler` 3.x with SQLAlchemy job store on the same SQLite
- **Container runtime**: Docker + docker-compose
- **Version control**: git, optional auto-push to GitHub

## Read this next

- `01-architecture.md` — components, processes, data flow
- `02-data-model.md` — SQLite schema and filesystem layout
- `03-pipeline-states.md` — state machine and transitions
- `10-knowledge-base.md` — what goes inside the wiki itself
