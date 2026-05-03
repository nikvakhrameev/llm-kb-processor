# CLAUDE.md — app/workers/

## Purpose

Long-running asyncio processes that poll SQLite and drive the pipeline.

## Files

| File           | Purpose |
|----------------|---------|
| `base.py`      | Generic `poll_loop(db, handler, interval)` — poll → handle → sleep |
| `resources.py` | Resource worker: parse `received` rows, gate `parsed` rows, transition states |
| `ingest.py`    | Ingest worker: claude-agent-sdk integration, MCP tools, git ops, Docker-ready |

## Resource worker (`resources.py`)

Polls for `status IN ('received', 'parsed')`. Dispatches by status:

- **received** → transition to `parsing`, call `parse()`, write result, transition to `parsed`
- **parsed** → transition to `gating`, call `gate_with_retries()`, score≥60 → `approved`, score<60 → `rejected`

Error handling:
- `ParseError` → terminal `failed` (no retry)
- `TransientParseError` / unknown → retry with backoff, max 3 attempts
- Gate failure after 3 retries → default-accept (score=65, `quality_gate_skipped=true`)

## Ingest worker (`ingest.py`)

Polls for `status = 'approved'`. Processes **one at a time** (process-wide asyncio lock).

### MCP tool: `report_result`

```python
@tool("report_result", "Report final result...", {
    "status": str,         # "success" | "partial" | "failed"
    "pages_created": list, # paths relative to kb_root
    "pages_updated": list,
    "log_entry": str,      # one-line markdown for log.md
    "summary": str,        # 1-2 sentences for notification
    "warnings": list,
})
```

The worker captures this from the message stream, not the return value.

### Bash hook

Regex allowlist for git commands only (`git status|diff|add|commit|log|rev-parse` + `ls|cat|wc`).
Any other Bash command is denied via `PreToolUse` hook.

### ClaudeAgentOptions

- `cwd` = wiki repo root
- `system_prompt` = `claude_code` preset
- `setting_sources` = `["project"]` → auto-loads `CLAUDE.md` from cwd
- `permission_mode` = `acceptEdits`
- `allowed_tools` = Read, Write, Edit, Grep, Glob, Bash, `mcp__kb_ingest__report_result`
- `env` = `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` (pointed at DeepSeek)
- `hooks` = `PreToolUse` with Bash allowlist

### Git workflow

1. `ensure_clean()` — auto-commit dirty tree as `manual: pre-ingest snapshot`
2. Run agent
3. Capture `report_result` from tool use blocks
4. Verify HEAD commit has expected prefix (`ingest:`, `lint:`, `synthesis:`)
5. On success → transition to `done`, notify
6. On failure → `rollback()` (git reset --hard + clean), retry

### Dispatch by resource type

- `web|pdf|youtube|text|voice|md` → `render_ingest()` prompt
- `_lint` → `render_lint()` prompt, commit prefix `lint:`
- `_synthesis_weekly` → `render_synthesis()` prompt, commit prefix `synthesis:`

## Concurrency

- Both workers use `asyncio.sleep(interval)` between polls (2s default)
- Resource worker processes one resource per tick, sequential
- Ingest worker holds `asyncio.Lock` — strictly one ingest at a time
- CAS guard (`WHERE status=?`) prevents cross-process double-processing
