# CLAUDE.md — app/ Internals

## Module map

| File                 | Purpose |
|----------------------|---------|
| `settings.py`        | pydantic-settings singleton, all ~30 config params from `.env` |
| `db.py`              | SQLite wrapper: migrations, CRUD, CAS transitions, retry, poll, sweeper |
| `models.py`          | Dataclasses: `Resource`, `ParseResult`, `GateResult`, `IngestOutcome` |
| `enums.py`           | `ResourceType` (StrEnum), `ResourceStatus` (StrEnum) |
| `utils.py`           | `uuid_str()`, `make_slug()`, `utc_now_iso()`, `truncate_to_tokens()`, `extract_urls()`, `is_youtube_url()`, `extract_video_id()` |
| `prompts.py`         | All LLM prompt templates as constants + `render_*()` functions |
| `quality_gate.py`    | DeepSeek client (OpenAI-compat), `gate_with_retries()`, default-accept |
| `notifier.py`        | `notify_terminal()` — stdout + optional Telegram, idempotent on `notification_sent_at` |
| `bot.py`             | aiogram dispatcher, auth middleware, `run_bot()` entry |
| `scheduler.py`       | APScheduler with SQLAlchemy job store, 3 jobs + `run_scheduler()` entry |
| `worker_resources.py`| Thin entry: `python -m app.worker_resources` |
| `worker_ingest.py`   | Thin entry: `python -m app.worker_ingest` |

## Subpackages

| Package     | Contents |
|-------------|----------|
| `parsers/`  | Parser protocol + 6 type-specific parsers + dispatch |
| `workers/`  | `base.py` (poll loop), `resources.py` (parse+gate), `ingest.py` (claude-agent-sdk) |
| `handlers/` | Telegram bot handlers: `start.py`, `status.py`, `messages.py` |
| `cron/`     | `sweeper.py`, `lint.py`, `synthesis.py` — enqueue synthetic resource rows |
| `ingest/`   | Reserved for future ingest sub-modules |

## Conventions

- **Singletons**: `settings` (imported everywhere), Whisper model (lazy in `parsers/voice.py`)
- **Async**: Workers use `asyncio` poll loops. Sync libs (trafilatura, pymupdf4llm, whisper) wrapped in `asyncio.to_thread()`
- **DB access**: Workers open their own SQLite connection. `db.transition()` uses CAS guard (`WHERE status=?`) to prevent races
- **Imports**: `from app.X import Y` — flat module references, no deep nesting
- **Entry points**: Thin wrappers (`worker_resources.py`, `worker_ingest.py`) that call `asyncio.run()` on the main worker function
- **No circular imports**: `settings` depends on nothing. `enums` + `models` depend on nothing. `db` depends on `enums`. Higher layers depend down

## Adding a new module

1. Keep it focused. Split into subpackage if the file grows large
2. Add tests in `tests/test_<module>.py`
3. If it's a new worker, add a thin entry point at `app/worker_<name>.py`
4. If it uses LLMs, add prompts to `prompts.py` with `render_*()` function
5. If it needs config, add to `settings.py` and `.env.example`
