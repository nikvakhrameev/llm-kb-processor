"""Resource worker — parses raw inputs and runs the quality gate.

Polls SQLite for resources with status 'received' or 'parsed',
dispatches to the appropriate parser (for received) or quality gate
(for parsed), and transitions states accordingly.
"""

import json
import traceback
from pathlib import Path

from app.db import Database
from app.enums import ResourceStatus
from app.models import Resource
from app.notifier import notify_terminal
from app.parsers.base import ParseError, TransientParseError
from app.parsers.dispatch import parse
from app.quality_gate import gate_with_retries
from app.settings import settings
from app.workers.base import poll_loop

# In-flight statuses that this worker handles
INFLIGHT_STATUSES = [ResourceStatus.RECEIVED, ResourceStatus.PARSED]


def _row_to_resource(row) -> Resource:
    """Convert a sqlite3.Row to a Resource dataclass."""
    d = dict(row)
    # Convert integer boolean
    if "quality_gate_skipped" in d:
        d["quality_gate_skipped"] = bool(d["quality_gate_skipped"])
    return Resource(**d)


async def handle_next(db: Database) -> None:
    """Poll for the next ready resource and process it."""
    rows = db.poll_resources(INFLIGHT_STATUSES)
    if not rows:
        return

    row = rows[0]
    resource = _row_to_resource(row)

    if resource.status == ResourceStatus.RECEIVED:
        await handle_parse(db, resource)
    elif resource.status == ResourceStatus.PARSED:
        await handle_gate(db, resource)


async def handle_parse(db: Database, resource: Resource) -> None:
    """Parse a received resource and transition to parsed."""
    rid = resource.id
    try:
        db.transition(rid, ResourceStatus.RECEIVED, ResourceStatus.PARSING)
    except RuntimeError:
        return  # CAS race — another worker got it

    try:
        result = await parse(resource, settings.kb_root)
    except ParseError as e:
        db.transition(rid, ResourceStatus.PARSING, ResourceStatus.FAILED,
                      error_message=str(e))
        notify_terminal(db, rid)
        return
    except TransientParseError as e:
        ok = db.schedule_retry(rid, ResourceStatus.RECEIVED, str(e),
                               max_retries=settings.retries_max,
                               backoff_base_seconds=settings.retry_backoff_base_seconds)
        if not ok:
            db.transition(rid, ResourceStatus.PARSING, ResourceStatus.FAILED,
                          error_message=f"retries exhausted: {e}")
            notify_terminal(db, rid)
        return
    except Exception as e:
        ok = db.schedule_retry(rid, ResourceStatus.RECEIVED, str(e),
                               max_retries=settings.retries_max,
                               backoff_base_seconds=settings.retry_backoff_base_seconds)
        if not ok:
            db.transition(rid, ResourceStatus.PARSING, ResourceStatus.FAILED,
                          error_message=f"retries exhausted: {e}")
            notify_terminal(db, rid)
        return

    # Parse succeeded
    db.update_resource(
        rid,
        status=ResourceStatus.PARSED,
        parsed_text_path=result.parsed_path,
        content_title=result.title,
    )
    # Write event for parsed
    db.execute(
        "INSERT INTO events (resource_id, event_type, payload) VALUES (?, 'parsed', ?)",
        (rid, json.dumps({"title": result.title, "char_count": result.char_count,
                          "parser": result.parser_id})),
    )
    db.conn.commit()


async def handle_gate(db: Database, resource: Resource) -> None:
    """Run the quality gate on a parsed resource."""
    rid = resource.id
    try:
        db.transition(rid, ResourceStatus.PARSED, ResourceStatus.GATING)
    except RuntimeError:
        return  # CAS race

    # Read parsed file body
    parsed_path = settings.kb_root / (resource.parsed_text_path or "")
    if not parsed_path.exists():
        db.transition(rid, ResourceStatus.GATING, ResourceStatus.FAILED,
                      error_message=f"parsed file not found: {resource.parsed_text_path}")
        notify_terminal(db, rid)
        return

    body = parsed_path.read_text(encoding="utf-8")

    # Read purpose.md for context
    purpose_path = settings.kb_root / "purpose.md"
    purpose_md = purpose_path.read_text(encoding="utf-8") if purpose_path.exists() else ""

    source = resource.source_url or resource.original_file_path or "(inline text)"
    title = resource.content_title or ""

    try:
        gate_result, skipped = await gate_with_retries(
            resource.resource_type, source, title, body, purpose_md,
            max_retries=settings.retries_max,
        )
    except Exception as e:
        ok = db.schedule_retry(rid, ResourceStatus.PARSED, str(e),
                               max_retries=settings.retries_max,
                               backoff_base_seconds=settings.retry_backoff_base_seconds)
        if not ok:
            # Default-accept on all-gate-failures
            db.transition(rid, ResourceStatus.GATING, ResourceStatus.APPROVED,
                          quality_gate_skipped=True,
                          quality_score=65,
                          quality_rationale=f"gate infrastructure failure: {e}",
                          quality_topics=json.dumps([]))
            return
        return

    # Write gate result event
    db.execute(
        "INSERT INTO events (resource_id, event_type, payload) VALUES (?, 'gate_result', ?)",
        (rid, json.dumps({
            "score": gate_result.score,
            "rationale": gate_result.rationale,
            "topics": gate_result.topics,
            "skipped": skipped,
        })),
    )

    if gate_result.score >= settings.gate_accept_threshold:
        db.transition(rid, ResourceStatus.GATING, ResourceStatus.APPROVED,
                      quality_score=gate_result.score,
                      quality_rationale=gate_result.rationale,
                      quality_topics=json.dumps(gate_result.topics),
                      quality_gate_skipped=skipped)
    else:
        # Move parsed file to rejected
        if resource.parsed_text_path:
            src = settings.kb_root / resource.parsed_text_path
            dst = settings.kb_root / "raw" / "rejected" / Path(resource.parsed_text_path).name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                src.rename(dst)

        db.transition(rid, ResourceStatus.GATING, ResourceStatus.REJECTED,
                      quality_score=gate_result.score,
                      quality_rationale=gate_result.rationale,
                      quality_topics=json.dumps(gate_result.topics))
        notify_terminal(db, rid)


async def run_resource_worker() -> None:
    """Entry point for the resource worker."""
    db = Database(settings.state_db)
    db.connect()
    db.run_migrations()
    await poll_loop(db, handle_next, settings.poll_interval_seconds,
                    name="resource-worker")
