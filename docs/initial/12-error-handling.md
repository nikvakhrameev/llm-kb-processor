# Error Handling

This doc collects all retry, backoff, and failure-notification rules in one
place. Individual stage docs reference these policies; this is the canonical
spec.

## Principles

1. **Never lose a resource silently.** Every resource ends in one of three
   terminal states (`done`, `failed`, `rejected`) and every terminal
   transition produces a Telegram notification.
2. **Crash-safe by default.** Workers can be killed at any time without
   leaving the system in an inconsistent state. The sweeper plus
   transactional state changes guarantee recovery.
3. **Conservative retries.** Three attempts per stage, exponential backoff,
   and the gate stage uses default-accept on infrastructure failure rather
   than rejecting the resource.
4. **Wiki transactionality.** Either an ingest commits cleanly or the wiki
   is restored to its previous SHA. Half-applied wiki changes are not
   allowed.

## Stage-by-stage failure matrix

### Bot (intake)

| Failure                                | Action                                                |
|----------------------------------------|-------------------------------------------------------|
| Telegram getFile / download fails      | Reply error, do not insert SQLite row                 |
| Disk write fails (raw/inbox/)          | Reply error, do not insert SQLite row                 |
| SQLite insert fails                    | Reply "system error, try again", log                  |
| User not in allowlist                  | Silent drop                                           |
| Unsupported message type               | Reply with supported-types message                    |

### Resource worker — parsing

| Failure                                | Type             | Action                                |
|----------------------------------------|------------------|---------------------------------------|
| `ParseError` (404, empty, corrupt)     | terminal         | status=`failed`, no retry, notify     |
| `TransientParseError` (rate limit, 5xx)| transient        | retry up to 3x with backoff           |
| Network timeout                        | transient        | retry                                 |
| Worker crash mid-parse                 | sweeper recovery | status reset to `received` after 30m  |
| Disk full when writing parsed file     | transient        | retry; if 3x fails, terminal `failed` |
| Unknown exception                      | transient        | retry; if 3x fails, terminal `failed` |

### Resource worker — quality gate

| Failure                                | Action                                                  |
|----------------------------------------|---------------------------------------------------------|
| DeepSeek 5xx, network, rate limit      | retry up to 3x with backoff                             |
| All 3 retries fail                     | **default-accept**: status=`approved`, gate_skipped=1   |
| DeepSeek returns invalid JSON          | retry up to 3x; if all fail, default-accept             |
| DeepSeek returns score outside 0..100  | clamp to range, log warning, proceed                    |
| Gate score < threshold                 | status=`rejected`, terminal, notify with rationale      |

The default-accept policy is a deliberate choice. Rationale: the user
explicitly sent this resource, so the cost of losing it is higher than the
cost of ingesting one of unknown quality. The flag in the audit log lets
you spot patterns later.

### Ingest worker

| Failure                                | Action                                                  |
|----------------------------------------|---------------------------------------------------------|
| Anthropic API 5xx, network, rate limit | retry up to 3x with backoff                             |
| Container OOM                          | rollback git, retry; if persistent, terminal `failed`   |
| Hit `max_turns` without report_result  | rollback git, retry; if 3x, terminal `failed`           |
| `report_result` not called             | rollback git, retry                                     |
| `report_result.status == "failed"`     | rollback git, retry once; then terminal `failed`        |
| Commit prefix wrong (not `ingest:`)    | rollback git to `before_sha`, retry                     |
| Bash hook denied a needed command      | the agent retries within `max_turns`; no extra worker action |
| Timeout (>10 min)                      | cancel async iter, rollback git, retry                  |
| Worker crash mid-ingest                | sweeper resets to `approved` after 30m, retry           |
| Git working tree dirty pre-flight      | auto-commit as `manual: pre-ingest snapshot`, proceed   |

## Retry semantics

```python
RETRIES_MAX = 3
RETRY_BACKOFF_BASE_SECONDS = 60   # → 60s, 120s, 240s

def schedule_retry(rid: str, prev_status: str, error: str) -> bool:
    """Returns True if a retry was scheduled, False if exhausted."""
    with db.transaction():
        row = db.fetchone("SELECT retry_count FROM resources WHERE id=?", (rid,))
        if row.retry_count >= RETRIES_MAX:
            return False
        delay = RETRY_BACKOFF_BASE_SECONDS * (2 ** row.retry_count)
        db.execute("""
            UPDATE resources
               SET status = ?,
                   retry_count = retry_count + 1,
                   next_attempt_at = datetime('now', ?),
                   error_message = ?,
                   updated_at = datetime('now')
             WHERE id = ?
        """, (prev_status, f"+{delay} seconds", error, rid))
        db.execute("INSERT INTO events(resource_id, event_type, payload) VALUES (?,?,?)",
                   (rid, "retry_scheduled",
                    json.dumps({"prev_status": prev_status, "delay_s": delay,
                                "attempt": row.retry_count + 1, "error": error})))
        return True
```

Note that `retry_count` is **never reset** during a resource's lifetime. A
resource that fails parsing twice and then succeeds at quality-gate has
`retry_count=2` going into ingest, with only 1 ingest retry remaining. This
prevents infinitely-retrying resources from stalling the queue across
multiple stages.

## The sweeper

A scheduler job runs every 5 minutes:

```sql
-- Reset stuck rows
UPDATE resources
   SET status = CASE status
                  WHEN 'parsing'   THEN 'received'
                  WHEN 'gating'    THEN 'parsed'
                  WHEN 'ingesting' THEN 'approved'
                END,
       retry_count = retry_count + 1,
       updated_at = datetime('now'),
       error_message = COALESCE(error_message, '') || ' [sweeper:stuck]'
 WHERE status IN ('parsing', 'gating', 'ingesting')
   AND datetime(updated_at) < datetime('now', '-30 minutes');

-- Log
INSERT INTO events(resource_id, event_type, payload)
SELECT id, 'sweeper_reset', json_object('from_status', status)
  FROM resources
 WHERE status IN ('received', 'parsed', 'approved')
   AND error_message LIKE '%[sweeper:stuck]%'
   AND datetime(updated_at) > datetime('now', '-1 minute');
```

The "1 hour" threshold is generous. Real ingests should complete in under
10 minutes; if a row sits in an in-flight state for 30+ minutes, the
worker either crashed or is wedged.

## Notification idempotency

Notifications are tied to the `notification_sent_at` column. The
notification function is structured to be safe under retries:

```python
async def notify_terminal(rid: str):
    row = db.fetchone("""
        SELECT notification_sent_at, status, telegram_chat_id, telegram_message_id,
               ingest_summary, error_message, quality_score, quality_rationale
          FROM resources WHERE id = ?
    """, (rid,))

    if row.notification_sent_at is not None:
        return  # already sent

    if row.telegram_chat_id is None:
        # system row (lint/synthesis); use OWNER_CHAT_ID
        text = format_system_notification(row)
        msg = await bot.send_message(settings.owner_chat_id, text)
    else:
        text = format_terminal_notification(row)
        msg = await bot.send_message(
            row.telegram_chat_id, text,
            reply_to_message_id=row.telegram_message_id,
        )

    db.execute("""
        UPDATE resources
           SET notification_sent_at = datetime('now'),
               updated_at = datetime('now')
         WHERE id = ? AND notification_sent_at IS NULL
    """, (rid,))
```

If the Telegram API call succeeds but the SQLite update fails (very
unlikely), the next sweeper-driven retry would send the message again. We
accept this rare double-send as preferable to dropped notifications.

## Notification format examples

### Done

```
✅ Ingested
"Great Article on Foo"
+2 pages, 1 update · score 82
Topics: llm-agents, evals
Commit: abc1234
```

### Done with warnings

```
✅ Ingested (with notes)
"Great Article on Foo"
+2 pages, 1 update · score 82
⚠️  Contradicts existing claim in [[entities/Andrej-Karpathy]] — flagged
Commit: abc1234
```

### Rejected

```
🚫 Skipped (low quality, score 22)
"Cookie consent — Example.com"
Reason: parsed content is mostly cookie banner text, no substantive content
```

### Failed

```
❌ Ingest failed permanently after 3 attempts
"Great Article on Foo"
Last error: timeout (>10 min) during agent run
Resource id: r1 — use /status r1 for details
```

## Observability

Every retry, sweep, and notification is an `events` row. Useful queries:

```sql
-- All currently-stuck resources
SELECT id, resource_type, status, retry_count, error_message, updated_at
  FROM resources
 WHERE status NOT IN ('done', 'failed', 'rejected')
   AND datetime(updated_at) < datetime('now', '-15 minutes');

-- Failure rate by stage over the past 7 days
SELECT
    json_extract(payload, '$.from') AS from_stage,
    count(*) AS n
  FROM events
 WHERE event_type = 'status_change'
   AND json_extract(payload, '$.to') = 'failed'
   AND created_at > datetime('now', '-7 days')
 GROUP BY from_stage;

-- Quality gate skip rate
SELECT count(*) FROM resources WHERE quality_gate_skipped = 1;
```

These can be exposed as `/health` or `/metrics` Telegram commands later.

## Manual recovery

Any stuck resource can be force-rerun by setting it back to a pickable
status:

```sql
UPDATE resources
   SET status = 'received', next_attempt_at = NULL,
       retry_count = 0, error_message = NULL
 WHERE id = '...';
```

The next worker poll picks it up and runs the pipeline from scratch. The
previous parsed file (if any) is overwritten; existing events stay in the
audit log.

For ingest specifically, also verify the wiki git status is clean before
manual rerun, otherwise the agent's pre-flight will create an unwanted
`manual:` commit.

## Limits worth monitoring

| Limit                              | Default | Where          |
|-----------------------------------|---------|----------------|
| Max retries per stage              | 3       | settings       |
| Backoff base                       | 60s     | settings       |
| Ingest agent timeout               | 600s    | settings       |
| Ingest agent max turns             | 25      | settings       |
| Lint max turns                     | 40      | settings       |
| Synthesis max turns                | 60      | settings       |
| Sweeper stuck threshold            | 30 min  | settings       |
| Telegram document download         | 20 MB   | Telegram API   |
| Quality gate snippet               | 2000 tok | settings       |

All defaults are environment-configurable so tuning does not require a
code change.
