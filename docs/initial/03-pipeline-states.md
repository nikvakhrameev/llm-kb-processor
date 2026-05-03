# Pipeline States

The `resources.status` column drives all worker dispatch. The state machine is
linear with explicit retry loops and two terminal states.

## States

| State            | Set by             | Means                                              |
|------------------|--------------------|----------------------------------------------------|
| `received`       | bot                | Row inserted, raw input saved (file or inline text) |
| `parsing`        | resource worker    | Worker is currently parsing this row               |
| `parsed`         | resource worker    | Parsed text is on disk, ready for quality gate     |
| `gating`         | resource worker    | Worker is currently calling DeepSeek               |
| `approved`       | resource worker    | Quality gate passed (or skipped after retries)     |
| `rejected`       | resource worker    | Quality gate scored below threshold (terminal)     |
| `ingesting`      | ingest worker      | Claude Agent SDK is currently running              |
| `done`           | ingest worker      | Wiki updated and committed (terminal)              |
| `failed`         | any worker         | All retries exhausted (terminal)                   |

## State transitions

```
                     ┌─────────────┐
                     │  received   │  (bot)
                     └──────┬──────┘
                            │
                  resource worker picks up
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
                  resource worker picks up
                            ▼
                     ┌─────────────┐
                     │  gating     │
                     └──────┬──────┘
              ┌─────────────┼──────────────┐
       score<60         60≤score≤100   3 errors
              │             │              │
              ▼             ▼              ▼
       ┌──────────┐  ┌──────────┐    skip-and-accept
       │ rejected │  │ approved │←───────┘
       └──────────┘  └─────┬────┘  (sets quality_gate_skipped=1)
        (terminal)         │
                  ingest worker picks up
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

## Polling queries

### Resource worker

```sql
SELECT id FROM resources
 WHERE status IN ('received', 'parsed')
   AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))
 ORDER BY created_at
 LIMIT 1;
```

The worker dispatches by status:
- `received` → parsing pipeline → `parsed` or retry/failed
- `parsed` → quality gate → `approved`/`rejected` or retry

Both `received` and `parsed` are picked up by the same worker so a single
resource progresses through parsing and gating in close succession with no
extra coordination.

### Ingest worker

```sql
SELECT id FROM resources
 WHERE status = 'approved'
   AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))
 ORDER BY created_at
 LIMIT 1;
```

Always selects exactly one. The ingest worker holds a process-wide asyncio
lock during ingest so concurrent polls do not double-pick (the SQL alone
does not prevent a second worker, but in MVP there is one ingest worker by
design).

## Status transition rules

All status changes happen in a single SQLite transaction that also writes an
`events` row. This guarantees that an external observer can always reconstruct
the lifecycle even if a worker crashes.

```python
def transition(resource_id, from_status, to_status, **fields):
    with db.transaction():
        n = db.execute("""
            UPDATE resources
               SET status = ?, updated_at = datetime('now'),
                   <fields>
             WHERE id = ? AND status = ?
        """, (to_status, resource_id, from_status))
        assert n == 1, f"race: expected status={from_status}"
        db.execute("""
            INSERT INTO events (resource_id, event_type, payload)
            VALUES (?, 'status_change', ?)
        """, (resource_id, json.dumps({"from": from_status, "to": to_status, **fields})))
```

The `WHERE status = ?` predicate is a CAS guard against two workers picking
the same row.

## Retry policy

Each terminal-on-failure transition (`parsing → failed`, `gating → failed`,
`ingesting → failed`) is preceded by retry attempts. The retry mechanism is
identical across stages:

```python
RETRIES_MAX = 3
BACKOFF_BASE_SECONDS = 60   # 60s, 120s, 240s

def schedule_retry(resource_id, prev_status, error):
    row = db.fetchone("SELECT retry_count FROM resources WHERE id=?", (resource_id,))
    if row.retry_count >= RETRIES_MAX:
        transition(resource_id, current, "failed", error_message=error)
        notify(resource_id, ok=False)
        return
    delay = BACKOFF_BASE_SECONDS * (2 ** row.retry_count)
    db.execute("""
        UPDATE resources
           SET status = ?, retry_count = retry_count + 1,
               next_attempt_at = datetime('now', ?),
               error_message = ?
         WHERE id = ?
    """, (prev_status, f"+{delay} seconds", error, resource_id))
```

Note that retrying means **rolling back to the previous queue-pickable
status** (e.g. `parsing` rolls back to `received`, `ingesting` rolls back to
`approved`), not staying in the in-flight status. This is critical for the
sweeper.

### Quality gate skip-and-accept

The quality gate is the **only** stage that does not fail terminally. After
3 DeepSeek failures, the resource is marked `approved` with
`quality_gate_skipped=1`. Rationale: losing a source the user explicitly sent
is worse than ingesting one of unknown quality.

## Stuck-row sweeper

The scheduler runs a sweeper every 5 minutes:

```sql
UPDATE resources
   SET status = CASE status
                  WHEN 'parsing' THEN 'received'
                  WHEN 'gating' THEN 'parsed'
                  WHEN 'ingesting' THEN 'approved'
                END,
       retry_count = retry_count + 1,
       error_message = 'sweeper: stuck for >30 minutes'
 WHERE status IN ('parsing', 'gating', 'ingesting')
   AND datetime(updated_at) < datetime('now', '-30 minutes');
```

This handles the case where a worker was killed mid-flight (OOM, container
restart, OS reboot). The sweeper is idempotent and safe to run while workers
are healthy because healthy workers update `updated_at` regularly during long
operations (parser progress, Claude SDK heartbeat).

## Notifications and idempotency

Notifications are sent only when transitioning to a terminal state (`done`,
`failed`, `rejected`) and only if `notification_sent_at IS NULL`. The
notification function sets `notification_sent_at` in the same transaction as
the Telegram API call's success acknowledgment. If the Telegram API call
fails, the field stays NULL and the next sweeper run retries the
notification.

System rows (`_lint`, `_synthesis_weekly`) skip the parsing/gating stages and
are inserted directly with status=`approved`. They have no
`telegram_message_id`, so notifications are sent as new messages to a
configured `OWNER_CHAT_ID` rather than as replies.

## Forbidden transitions

The transition function asserts the `from_status` precondition. Any attempt
to move outside the documented graph (e.g. `done → ingesting`) raises and is
logged as `events.event_type='invalid_transition'`. There is no automatic
recovery; manual SQL is required.
