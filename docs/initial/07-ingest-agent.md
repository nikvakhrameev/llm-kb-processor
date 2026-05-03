# Ingest Agent

## Role

The ingest agent is a Claude model invoked through the `claude-agent-sdk`
Python package. It is the only component allowed to mutate the wiki. Its
responsibilities:

1. Read the parsed source file.
2. Read the wiki schema (`CLAUDE.md`, `purpose.md`, `index.md`) and any
   wiki pages that look related.
3. Create a `wiki/sources/<slug>.md` page summarizing the source.
4. Update or create entity/concept pages affected by the new source.
5. Append a one-line entry to `log.md`.
6. Stage and commit all changes with a `ingest:` prefix.
7. Call the `report_result` MCP tool with a structured summary.

## SDK choice

We use `claude-agent-sdk` (Python) rather than the CLI subprocess for these
reasons:

- Native async — integrates with our asyncio worker without spawning a child
  process per ingest.
- Typed `ClaudeAgentOptions` instead of stringly-typed flags.
- Structured message stream — tool uses arrive as objects, no JSON parsing
  of CLI stdout.
- In-process MCP custom tools via `@tool` and `create_sdk_mcp_server` —
  required to receive the structured `report_result` payload.
- Clean cancellation semantics on timeout.

## Custom tool: `report_result`

The agent **must** call `report_result` exactly once at the end of its run.
This is the structured contract between agent and worker.

```python
from claude_agent_sdk import tool, create_sdk_mcp_server, ClaudeAgentOptions, query

@tool(
    "report_result",
    "Report final result of ingest. Call exactly once when finished.",
    {
        "status":         str,        # "success" | "partial" | "failed"
        "pages_created":  list,       # list[str], paths relative to kb_root
        "pages_updated":  list,       # list[str]
        "log_entry":      str,        # one-line markdown bullet for log.md
        "summary":        str,        # 1-2 sentences for Telegram reply
        "warnings":       list,       # list[str], e.g. contradictions found
    },
)
async def report_result(args: dict) -> dict:
    # The worker captures this via the message stream.
    # Returning anything is fine; the worker reads from the message, not the return.
    return {"content": [{"type": "text", "text": "ack"}]}

ingest_mcp = create_sdk_mcp_server(
    name="kb_ingest",
    version="1.0.0",
    tools=[report_result],
)
```

The same MCP server can host additional internal tools (e.g.
`get_resource_metadata` to fetch the parsed file's frontmatter without
shelling out). Keep them few — the principle is "agent uses standard
filesystem tools for everything except final reporting".

## Allowed tools

```python
ALLOWED_TOOLS = [
    # Built-in filesystem and search
    "Read", "Write", "Edit", "Grep", "Glob",
    # Bash, restricted by allowed_bash_patterns below
    "Bash",
    # Custom MCP server
    "mcp__kb_ingest__report_result",
]
```

`Bash` is intentionally allowed because the agent must run `git add`,
`git commit`, `git status`, `git diff`. We restrict it via SDK hooks (see
"Hooks" below) to a fixed allowlist.

`Write` and `Edit` are scoped to the wiki by the working directory plus the
container's read-only mount of `raw/parsed/`. The SDK does not enforce
filesystem scoping itself; that is done by Docker (see "Isolation" below).

Tools **not** allowed:

- `WebFetch`, `WebSearch` — the wiki is built from sources the user
  explicitly provides, not from arbitrary internet content.
- `Task` (sub-agents) — adds cost and unpredictability for no MVP benefit.
- `NotebookRead`/`NotebookEdit` — irrelevant to markdown wiki.

## Bash command policy (hook-enforced)

Even with `Bash` enabled, only the following commands are permitted, enforced
by a `PreToolUse` hook:

```python
ALLOWED_BASH_PATTERNS = [
    r"^git status( -.+)?$",
    r"^git diff( --cached)?( --stat)?( -- .+)?$",
    r"^git add (-A|--all|[\w./\-_]+( [\w./\-_]+)*)$",
    r"^git commit -m '[^']+'$",
    r"^git log( --oneline)?( -n \d+)?$",
    r"^git rev-parse HEAD$",
    r"^ls( -la)?( [\w./\-_]+)?$",
    r"^cat [\w./\-_]+$",            # redundant with Read but agents reach for it
    r"^wc -l [\w./\-_]+$",
]

async def bash_pre_hook(input_data, tool_use_id, context):
    if input_data["tool_name"] != "Bash":
        return {}
    cmd = input_data["tool_input"].get("command", "").strip()
    if any(re.match(p, cmd) for p in ALLOWED_BASH_PATTERNS):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Bash command not in allowlist: {cmd!r}",
        }
    }
```

This blocks the agent from running anything destructive (`rm`, `find`,
`curl`, etc.) even by accident or by a prompt-injection attempt embedded in
the source content.

## Invocation

```python
from claude_agent_sdk import query, ClaudeAgentOptions, HookMatcher

options = ClaudeAgentOptions(
    cwd=str(WORKSPACE_DIR),                  # /workspace inside container
    system_prompt={"type": "preset", "preset": "claude_code"},  # use CC's default base
    setting_sources=["project"],             # auto-load CLAUDE.md from cwd
    permission_mode="acceptEdits",           # auto-approve Edit/Write within cwd
    allowed_tools=ALLOWED_TOOLS,
    mcp_servers={"kb_ingest": ingest_mcp},
    max_turns=25,
    hooks={
        "PreToolUse": [HookMatcher(matcher="Bash", hooks=[bash_pre_hook])],
    },
    model="claude-sonnet-4-7",   # see "Model selection" below
)

prompt = build_ingest_prompt(resource, parsed_relpath)

async with timeout(seconds=600):  # 10 min hard cap
    async for message in query(prompt=prompt, options=options):
        yield message    # consumed by worker
```

The worker awaits the async iterator and processes each `message` to find
the `report_result` tool use and the final `ResultMessage`.

## Model selection

| Job                | Model                  | Why                                |
|--------------------|------------------------|------------------------------------|
| Ingest (per source)| `claude-sonnet-4-7`    | Workhorse: fast, cheap enough, high quality on focused agentic tasks |
| Daily lint         | `claude-sonnet-4-7`    | Same; lint reads many pages and is cost-sensitive |
| Weekly synthesis   | `claude-opus-4-7`      | Reads a week of log + many wiki pages, benefits from stronger reasoning |

Both are configured via `ClaudeAgentOptions.model`. Switching is one config
change.

## Capturing the result

```python
async def run_ingest(resource: Resource) -> IngestOutcome:
    report = None
    final_result = None

    async for msg in stream_ingest(resource):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock) and block.name == "mcp__kb_ingest__report_result":
                    report = block.input
        elif isinstance(msg, ResultMessage):
            final_result = msg

    if report is None:
        raise IngestError("agent did not call report_result")
    if final_result is None or final_result.is_error:
        raise IngestError(f"agent ended with error: {final_result}")

    # Verify the wiki actually committed
    head_sha = git_rev_parse_head(WORKSPACE_DIR)
    head_msg = git_log_subject(WORKSPACE_DIR, head_sha)
    if not head_msg.startswith("ingest:"):
        raise IngestError(f"no ingest commit found, HEAD subject: {head_msg!r}")

    return IngestOutcome(
        commit_sha=head_sha,
        report=report,
        cost_usd=final_result.total_cost_usd,
        duration_ms=final_result.duration_ms,
    )
```

## Pre-flight: clean working tree

Before invoking the agent the worker asserts the wiki has no uncommitted
changes. If dirty (manual edits in progress, leftover from previous failed
ingest), it commits them with `manual: pre-ingest stash` to preserve them
before letting the agent loose.

```python
def ensure_clean(repo: Path):
    out = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                         check=True, capture_output=True, text=True).stdout
    if out.strip():
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "manual: pre-ingest snapshot",
                        "--no-verify"], cwd=repo, check=True)
```

## Post-flight: rollback on failure

If `run_ingest` raises, the worker rolls back any half-applied changes:

```python
def rollback(repo: Path, before_sha: str):
    subprocess.run(["git", "reset", "--hard", before_sha], cwd=repo, check=True)
    subprocess.run(["git", "clean", "-fd"], cwd=repo, check=True)
```

This guarantees that the wiki is **always** in a clean state at the start of
each ingest. Combined with the commit-prefix check, this makes ingest
**transactional**: either a new `ingest:`-prefixed commit lands, or the wiki
is byte-identical to what it was before.

## Isolation: Docker

The ingest worker runs Claude Agent SDK inside a dedicated Docker container,
not on the host. The container is built from the `ingest-worker` image and
launched per-ingest:

```yaml
# docker-compose.yml fragment
ingest-worker:
  image: kb-bot/ingest-worker
  environment:
    - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
  volumes:
    - ./knowledge_base:/workspace                       # rw
    - ./knowledge_base/raw/parsed:/workspace/raw/parsed:ro  # belt-and-suspenders
  network_mode: kb-egress-only   # custom network restricting to api.anthropic.com
  read_only: true
  tmpfs:
    - /tmp:size=64m
  cap_drop:
    - ALL
  security_opt:
    - no-new-privileges:true
  user: "1000:1000"
```

Notes:

- `read_only: true` makes the entire root filesystem read-only except the
  explicit volumes and tmpfs. The agent can write only to `/workspace`.
- The custom network `kb-egress-only` is configured at the docker daemon
  level to allow outbound only to `api.anthropic.com:443`. See
  `11-deployment.md`.
- All capabilities dropped, no new privileges. The container cannot escape.
- Even if the agent tries `git push`, the network rules block it. The host
  scheduler does the push from outside the container.

## Container lifecycle

The ingest worker process runs **on the host**, not inside the container. It
spawns/uses the container per ingest via the Docker SDK or by execing a
helper script. Two design choices:

- **Long-lived container** (`docker-compose up -d ingest-worker`) with the
  Python worker as PID 1 inside it. The worker calls `claude-agent-sdk`
  directly. The container does not exit between ingests. **Chosen for MVP.**
- Per-ingest containers (`docker run --rm`). More isolation, but startup
  overhead (~1s) and harder to share an open SDK session.

Long-lived is simpler. The container's read-only-rootfs + no-network +
dropped-caps is already strong isolation.

## Failure modes

| Failure                                | Worker action                                                |
|----------------------------------------|--------------------------------------------------------------|
| Timeout (>10 min)                       | Cancel async iterator, rollback git, schedule retry          |
| `report_result` not called              | Treat as error, rollback, retry                              |
| Agent committed but no `ingest:` prefix | Rollback to pre-ingest SHA, retry                            |
| `report_result.status == "failed"`      | Rollback, retry (agent self-reported inability to ingest)    |
| Anthropic API 5xx or rate limit         | Rollback, retry with backoff                                 |
| `BashTool` denied via hook              | Agent retries with allowed command (within `max_turns`)      |
| Hooks throwing exceptions               | Treated as fatal; the agent run aborts, retry                |

After 3 retries the resource is set to `failed` with the last error message
in `error_message`.

## Logging

For each ingest, the worker writes:

- `events(event_type='ingest_started', payload={resource_id, cwd, model})`
- For each tool use the agent makes: `events(event_type='tool_use',
  payload={tool, input_summary})` (lossy summarization to avoid blowing up
  the events table; full transcript optional behind a flag)
- `events(event_type='ingest_finished', payload={status, cost_usd, duration_ms,
  commit_sha, num_pages_created, num_pages_updated})`

This gives you a complete record of what the agent did across runs without
storing entire transcripts.
