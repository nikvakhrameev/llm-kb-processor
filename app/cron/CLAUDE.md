# CLAUDE.md — app/cron/

## Purpose

Background jobs triggered by the scheduler (`app/scheduler.py`). Each job
enqueues a synthetic `resources` row or performs maintenance on the database.

## Files

| File           | Purpose |
|----------------|---------|
| `sweeper.py`   | Reset rows stuck in in-flight states for >30 min |
| `lint.py`      | Insert `_lint` resource row → picked up by ingest worker |
| `synthesis.py` | Insert `_synthesis_weekly` resource row → picked up by ingest worker |

## Schedule (configured in `app/scheduler.py`)

| Job                | Trigger          | What it does |
|--------------------|------------------|--------------|
| `daily_lint`       | 02:00 daily      | `enqueue_lint()` → `_lint` row with `status='approved'` |
| `weekly_synthesis` | Sun 09:00        | `enqueue_synthesis()` → `_synthesis_weekly` row |
| `sweeper`          | Every 5 minutes  | `run_sweeper()` → resets stuck rows |

## Sweeper mechanics

Resets rows where `status IN ('parsing','gating','ingesting')` and
`updated_at < now - 30 minutes`. Rolls back to the previous pickable status:

```
parsing   → received
gating    → parsed
ingesting → approved
```

Each reset increments `retry_count` and appends `[sweeper:stuck]` to `error_message`.

## Synthetic rows

`_lint` and `_synthesis_weekly` rows:
- Have `status='approved'` (skip parsing + gating)
- Have no `telegram_*` fields (notifications go to `OWNER_CHAT_ID`)
- Are dispatched by the ingest worker to different prompts (`render_lint()` / `render_synthesis()`)
- Use different `max_turns` (40 for lint, 60 for synthesis)

## Adding a new cron job

1. Create the enqueue function in `app/cron/<name>.py`
2. Register it in `app/scheduler.py` with a `CronTrigger`
3. If it needs a new resource type, add to `ResourceType` in `enums.py`
4. Add dispatch logic in `workers/ingest.py` `run_ingest()`
5. Add prompt in `prompts.py`
