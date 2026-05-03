# Cron Jobs

Two background jobs run on a schedule to keep the wiki coherent and
generative. Both reuse the ingest worker's Claude Agent SDK harness with
different prompts and a different commit prefix. They are not separate
processes — they enqueue synthetic `resources` rows that the ingest worker
picks up like any other approved resource.

## Daily lint

**Schedule**: every day at 02:00 local time.

**Goal**: keep the wiki internally consistent. Find:

- **Orphans**: pages with no incoming wikilinks (other than the source page
  that mentioned them once).
- **Stubs**: pages with very short bodies that should probably be merged or
  expanded.
- **Contradictions**: claims on a page that contradict claims on another
  page or within the same page.
- **Stale claims**: time-bound statements that are likely outdated (e.g.
  "the latest model is...") if the source is older than X months.
- **Duplicate concepts**: two pages describing the same idea under different
  names.
- **Dangling links**: `[[wikilinks]]` pointing to non-existent files.
- **Index drift**: `index.md` missing entries or listing files that no
  longer exist.

**Output**:

- A digest page at `wiki/syntheses/lint/<YYYY-MM-DD>.md` listing findings
  grouped by severity.
- Optional small auto-fixes: rebuild `index.md`, remove broken wikilinks
  (commented out, not deleted), tag pages with `<!-- lint:orphan -->`.
- A `lint:` prefixed git commit.
- A Telegram message to `OWNER_CHAT_ID` summarizing top findings and a
  link to the full digest page.

**Scheduling**:

```python
@scheduler.scheduled_job(CronTrigger(hour=2, minute=0))
async def daily_lint():
    db.insert_resource(
        id=uuid7(),
        resource_type="_lint",
        status="approved",
        # no telegram_* fields
    )
```

The ingest worker recognizes `_lint` and dispatches to the lint prompt
instead of the ingest prompt.

**Prompt**: see `09-prompts.md` § Lint.

**Model**: same as ingest (`claude-sonnet-4-7`).

**Max turns**: 40. Lint touches more files than ingest.

**Frequency tuning**: daily is the default. If you find lint runs are
finding nothing, switch to weekly. If you find too many issues piling up
between runs, switch to twice daily. The schedule is a single env var.

## Weekly synthesis

**Schedule**: every Sunday at 09:00 local time.

**Goal**: produce a "What I learned this week" page that integrates
everything ingested over the last 7 days into a coherent narrative,
highlighting:

- New entities and concepts that entered the wiki this week.
- Connections discovered between sources (e.g. three different sources
  mentioning the same approach).
- Open questions raised by sources but not yet resolved.
- A reading-list-of-the-week with one sentence on each ingested source.

**Output**:

- A new page `wiki/syntheses/weekly/<YYYY-Www>.md` (ISO week).
- Cross-links from concept and entity pages to the new synthesis where
  appropriate (the agent inserts these in the relevant pages' "Mentioned in
  syntheses" section).
- A `synthesis:` prefixed git commit.
- A Telegram message with the synthesis preview (first ~500 chars) and a
  link to the full page.

**Scheduling**:

```python
@scheduler.scheduled_job(CronTrigger(day_of_week="sun", hour=9, minute=0))
async def weekly_synthesis():
    db.insert_resource(
        id=uuid7(),
        resource_type="_synthesis_weekly",
        status="approved",
    )
```

**Prompt**: see `09-prompts.md` § Synthesis.

**Model**: `claude-opus-4-7`. Synthesis is the highest-leverage agent run
of the week — it's the part that turns a pile of summaries into a
compounding artifact. Worth the higher cost (~$0.50–2 per run).

**Max turns**: 60.

## Skipping when nothing happened

If no new sources were ingested in the past 7 days, weekly synthesis still
runs but produces a short page noting "no new ingestions this week" and
optionally cross-links to older syntheses. This keeps the cadence
predictable.

## Sweeper integration

Cron jobs go through the same `resources` table, so they benefit from the
same sweeper, retry policy, and isolation. If a synthesis run crashes, the
sweeper resets it from `ingesting` to `approved` 30 minutes later and the
ingest worker retries it.

## On-demand triggers (v2 nice-to-have)

Telegram commands for manual triggering:

- `/lint` — enqueue an immediate lint run.
- `/synthesize <topic>` — enqueue an ad-hoc topic synthesis (creates a row
  with `resource_type='_synthesis_topic'` and the topic in `inline_text`).

Not in MVP, but the schema and dispatcher already support it; only the
command handler is missing.

## Notification format

For lint:

```
🧹 Daily lint complete (2026-05-02)
- 3 orphans
- 2 stubs
- 1 dangling link
- 1 likely contradiction (see Andrej-Karpathy.md ↔ Tesla.md)
Full digest: wiki/syntheses/lint/2026-05-02.md (commit abc1234)
```

For synthesis:

```
📚 Weekly synthesis (W18, Apr 27 – May 3)
6 new sources, 4 new entities, 2 new concepts.
Theme of the week: "Agentic patterns for LLMs"

[first ~500 chars of the synthesis page]

Full page: wiki/syntheses/weekly/2026-W18.md (commit def5678)
```

Both messages are sent as new messages to `OWNER_CHAT_ID`, not replies.

## Cost ceiling

To prevent runaway agent costs, both jobs have a soft cap configured via
`max_turns` and a hard cap via the same 10-minute timeout the ingest worker
uses. If a run hits the timeout, it is treated as a failure and retried
once. After two failures the run is marked `failed` and a Telegram
notification reports the issue.
