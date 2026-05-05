"""Ingest worker — mutates the wiki via claude-agent-sdk.

Polls SQLite for approved resources, invokes Claude Agent SDK in an isolated
environment, captures the report_result tool call, verifies git commits,
and transitions resources to done or retries on failure.

The ingest worker holds a process-wide asyncio lock so at most one ingest
runs at a time, avoiding git races on the working tree.
"""

import asyncio
import base64
import json
import os
import re
import subprocess
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookMatcher,
    AssistantMessage,
    ResultMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from app.db import Database
from app.enums import ResourceStatus, ResourceType
from app.models import Resource
from app.notifier import notify_terminal
from app.prompts import render_ingest, render_lint, render_synthesis
from app.settings import settings
from app.utils import utc_now_iso
from app.workers.base import poll_loop

# ------------------------------------------------------------------
# MCP tool: report_result
# ------------------------------------------------------------------

REPORT_RESULT_SCHEMA = {
    "status": str,  # "success" | "partial" | "failed"
    "pages_created": list,  # list[str], paths relative to kb_root
    "pages_updated": list,  # list[str]
    "log_entry": str,  # one-line markdown bullet for log.md
    "summary": str,  # 1-2 sentences for notification
    "warnings": list,  # list[str]
}


@tool(
    "report_result",
    "Report final result of ingest. Call exactly once when finished.",
    REPORT_RESULT_SCHEMA,
)
async def report_result(args: dict) -> dict:
    """The worker captures this from the message stream, not the return value."""
    return {"content": [{"type": "text", "text": "ack"}]}


kb_ingest_mcp = create_sdk_mcp_server(
    name="kb_ingest",
    version="1.0.0",
    tools=[report_result],
)

# ------------------------------------------------------------------
# Bash hook — allowlist
# ------------------------------------------------------------------

# Safe git subcommands — read-only or revertable, no history rewrite, no remotes.
# Args are unrestricted: the agent runs in an isolated container scoped to the wiki repo.
_SAFE_GIT = r"^git (status|diff|log|show|add|commit|branch|rev-parse|rev-list|ls-files|ls-tree|cat-file|describe|tag|notes|blame)( .*)?$"

ALLOWED_BASH_PATTERNS = [
    _SAFE_GIT,
    r"^(ls|cat|wc|find|grep|head|tail)( .*)?$",
]


async def bash_pre_hook(input_data, tool_use_id, context):
    """Block any Bash command not in the allowlist."""
    if input_data.get("tool_name") != "Bash":
        return {}
    cmd = (input_data.get("tool_input", {}) or {}).get("command", "").strip()
    if any(re.match(p, cmd) for p in ALLOWED_BASH_PATTERNS):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Bash command not in allowlist: {cmd!r}",
        }
    }


# ------------------------------------------------------------------
# Git helpers
# ------------------------------------------------------------------

_SSH_KEY_PATH = Path.cwd() / "tmp" / "git-ssh-key"


def _git_ssh_env() -> dict:
    """Return env dict with GIT_SSH_COMMAND if an SSH key is configured.

    The key is stored base64-encoded in settings so it survives env-file
    round-trips without escaping issues.
    """
    key_b64 = settings.kb_git_ssh_key
    if not key_b64:
        return {}
    try:
        key = base64.b64decode(key_b64).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"KB_GIT_SSH_KEY is not valid base64-encoded UTF-8: {e}") from e
    _SSH_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SSH_KEY_PATH.write_text(key)
    _SSH_KEY_PATH.chmod(0o600)
    return {"GIT_SSH_COMMAND": f"ssh -i {_SSH_KEY_PATH} -o StrictHostKeyChecking=accept-new"}


def _git(cmd: list[str], cwd: Path, extra_env: dict | None = None) -> str:
    """Run a git command and return stripped stdout."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["git"] + cmd, cwd=cwd, check=True,
        capture_output=True, text=True, env=env,
    )
    return result.stdout.strip()


def _git_pull(repo: Path, ssh_env: dict) -> None:
    """Pull latest changes from the configured remote."""
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    print(f"pulling origin/{branch} into {repo}")
    _git(["pull", "origin", branch], repo, extra_env=ssh_env)


def _git_push(repo: Path, ssh_env: dict) -> None:
    """Push current branch to the configured remote."""
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    print(f"pushing to origin/{branch}")
    _git(["push", "origin", branch], repo, extra_env=ssh_env)


def ensure_clean(repo: Path, ssh_env: dict | None = None) -> str:
    """Pull latest, then snapshot dirty tree. Returns the current HEAD SHA.

    If dirty, auto-commits as 'manual: pre-ingest snapshot'.
    """
    if ssh_env:
        try:
            _git_pull(repo, ssh_env)
        except subprocess.CalledProcessError as e:
            print(f"git pull failed (continuing anyway): {e.stderr}")

    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo,
        check=True, capture_output=True, text=True,
    ).stdout
    if out.strip():
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "manual: pre-ingest snapshot", "--no-verify"],
            cwd=repo, check=True,
        )
    return _git(["rev-parse", "HEAD"], repo)


def rollback(repo: Path, before_sha: str) -> None:
    """Hard-reset the repo to a previous commit."""
    subprocess.run(["git", "reset", "--hard", before_sha], cwd=repo, check=True)
    subprocess.run(["git", "clean", "-fd"], cwd=repo, check=True)


# ------------------------------------------------------------------
# Ingest runner
# ------------------------------------------------------------------

_ingest_lock = asyncio.Lock()


def _row_to_resource(row) -> Resource:
    d = dict(row)
    if "quality_gate_skipped" in d:
        d["quality_gate_skipped"] = bool(d["quality_gate_skipped"])
    return Resource(**d)


async def run_ingest(db: Database, resource: Resource, ssh_env: dict) -> dict | None:
    """Run the Claude agent to ingest one resource.

    Returns the report_result payload on success, or None on failure.
    """
    repo = settings.kb_root
    before_sha = ensure_clean(repo, ssh_env)

    # Build prompt
    if resource.resource_type == ResourceType.LINT:
        today = utc_now_iso()[:10]
        prompt_text = render_lint(today)
        commit_prefix = f"lint: {today}"
        model = settings.deepseek_model
        max_turns = settings.lint_max_turns
    elif resource.resource_type == ResourceType.SYNTHESIS_WEEKLY:
        # Compute ISO week
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        week_end = now.strftime("%Y-%m-%d")
        iso_label = f"{now.year}-W{now.isocalendar().week:02d}"
        prompt_text = render_synthesis(week_start, week_end, iso_label)
        commit_prefix = f"synthesis: weekly {iso_label}"
        model = settings.deepseek_model
        max_turns = settings.synthesis_max_turns
    else:
        topics = json.loads(resource.quality_topics or "[]")
        parsed_relpath = resource.parsed_text_path or ""
        prompt_text = render_ingest(resource, parsed_relpath, topics)
        commit_prefix = f"ingest: {resource.content_title or resource.short_id}"
        model = settings.claude_agent_pro_model
        max_turns = settings.ingest_max_turns


    options = ClaudeAgentOptions(
        cwd=str(repo),
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=["project"],
        permission_mode="acceptEdits",
        allowed_tools=[
            "Read", "Write", "Edit", "Grep", "Glob", "Bash",
            "mcp__kb_ingest__report_result",
        ],
        mcp_servers={"kb_ingest": kb_ingest_mcp},
        max_turns=max_turns,
        model=model,
        hooks={
            "PreToolUse": [HookMatcher(matcher="Bash", hooks=[bash_pre_hook])],
        },
        env={
            "ANTHROPIC_BASE_URL": settings.anthropic_base_url,
            "ANTHROPIC_AUTH_TOKEN": settings.anthropic_auth_token,
            "ANTHROPIC_SMALL_FAST_MODEL": settings.claude_agent_small_model,
        },
    )

    report = None
    final_result = None
    try:
        stream = query(prompt=prompt_text, options=options).__aiter__()
        while True:
            try:
                msg = await asyncio.wait_for(
                    stream.__anext__(),
                    timeout=settings.ingest_timeout_seconds,
                )
            except StopAsyncIteration:
                break
            print(f"received msg from claude agent: {msg}")
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock) and \
                            block.name == "mcp__kb_ingest__report_result":
                        report = block.input
            elif isinstance(msg, ResultMessage):
                final_result = msg
    except asyncio.TimeoutError:
        print(f"timeout waiting for next message from claude agent, rolling back")
        rollback(repo, before_sha)
        return None

    # Validate results
    if report is None:
        print(f"no report message found in claude agent, rolling back")
        rollback(repo, before_sha)
        return None

    if final_result is None or final_result.is_error:
        print(f"final result contains error status {final_result}, rolling back")
        rollback(repo, before_sha)
        return None

    # Verify commit prefix
    head_sha = _git(["rev-parse", "HEAD"], repo)
    head_msg = _git(["log", "-1", "--format=%s"], repo)
    if not head_msg.startswith(commit_prefix.split(":")[0] + ":"):
        print(f"resulted commit has wrong format {head_msg}")

    # Push if configured
    if settings.kb_git_autopush and ssh_env:
        try:
            _git_push(repo, ssh_env)
        except subprocess.CalledProcessError as e:
            print(f"git push failed: {e.stderr}")

    return {
        **report,
        "commit_sha": head_sha,
        "cost_usd": final_result.total_cost_usd or 0.0,
        "duration_ms": final_result.duration_ms or 0,
    }


# ------------------------------------------------------------------
# Worker loop
# ------------------------------------------------------------------

async def handle_approved(db: Database, ssh_env: dict) -> None:
    """Poll for approved resources and process one at a time."""
    print(f"fetch resource with status {ResourceStatus.APPROVED}")
    rows = db.poll_resources([ResourceStatus.APPROVED])
    print(f"fetched {len(rows)} rows")

    if not rows:
        return

    row = rows[0]
    resource = _row_to_resource(row)
    rid = resource.id

    try:
        db.transition(rid, ResourceStatus.APPROVED, ResourceStatus.INGESTING)
    except RuntimeError:
        return  # CAS race

    async with _ingest_lock:
        try:
            result = await run_ingest(db, resource, ssh_env)
        except Exception as e:
            ok = db.schedule_retry(rid, ResourceStatus.APPROVED, str(e),
                                   max_retries=settings.retries_max,
                                   backoff_base_seconds=settings.retry_backoff_base_seconds)
            if not ok:
                db.transition(rid, ResourceStatus.INGESTING, ResourceStatus.FAILED,
                              error_message=f"ingest failed after retries: {e}")
                await notify_terminal(db, rid)
            return

    if result is None:
        ok = db.schedule_retry(rid, ResourceStatus.APPROVED, "agent did not complete",
                               max_retries=settings.retries_max,
                               backoff_base_seconds=settings.retry_backoff_base_seconds)
        if not ok:
            db.transition(rid, ResourceStatus.INGESTING, ResourceStatus.FAILED,
                          error_message="agent did not call report_result after retries")
            await notify_terminal(db, rid)
        return

    # Success
    db.transition(
        rid, ResourceStatus.INGESTING, ResourceStatus.DONE,
        ingest_commit_sha=result.get("commit_sha", ""),
        ingest_summary=json.dumps({
            "pages_created": result.get("pages_created", []),
            "pages_updated": result.get("pages_updated", []),
            "warnings": result.get("warnings", []),
        }),
        ingest_log_entry=result.get("log_entry", ""),
    )
    await notify_terminal(db, rid)


def ensure_wiki_repo(repo: Path, ssh_env: dict) -> None:
    """Clone the wiki repo if kb_root is not a git repository."""
    if (repo / ".git").is_dir():
        return
    remote = settings.kb_git_remote
    if not remote:
        raise RuntimeError(
            f"KB_ROOT ({repo}) is not a git repository and KB_GIT_REMOTE is not set"
        )
    print(f"cloning {remote} into {repo}")
    repo.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git(["clone", remote, str(repo)], repo.parent, extra_env=ssh_env)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"git clone failed (exit {e.returncode}): {e.stderr.strip()}"
        ) from e


async def run_ingest_worker() -> None:
    """Entry point for the ingest worker."""
    ssh_env = _git_ssh_env()
    ensure_wiki_repo(settings.kb_root, ssh_env)

    db = Database(settings.state_db)

    async def _handler(db: Database) -> None:
        await handle_approved(db, ssh_env)

    try:
        db.connect()
        db.run_migrations()
        await poll_loop(db, _handler, settings.poll_interval_seconds,
                        name="ingest-worker")
    finally:
        db.close()
