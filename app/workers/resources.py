"""Resource worker — parses raw inputs and runs the quality gate.

Polls SQLite for resources with status 'received' or 'parsed',
dispatches to the appropriate parser (for received) or quality gate
(for parsed), and transitions states accordingly.
"""

import json
import logging
import time
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

logger = logging.getLogger("resource-worker")
logger.setLevel(logging.INFO)
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S"
    ))
    logger.addHandler(h)

INFLIGHT_STATUSES = [ResourceStatus.RECEIVED, ResourceStatus.PARSED]


def _row_to_resource(row) -> Resource:
    d = dict(row)
    if "quality_gate_skipped" in d:
        d["quality_gate_skipped"] = bool(d["quality_gate_skipped"])
    return Resource(**d)


async def handle_next(db: Database) -> None:
    rows = db.poll_resources(INFLIGHT_STATUSES)
    if not rows:
        return

    row = rows[0]
    resource = _row_to_resource(row)
    short = resource.short_id
    logger.info("picked %s type=%s status=%s", short, resource.resource_type, resource.status)

    if resource.status == ResourceStatus.RECEIVED:
        await handle_parse(db, resource)
    elif resource.status == ResourceStatus.PARSED:
        await handle_gate(db, resource)


async def handle_parse(db: Database, resource: Resource) -> None:
    rid = resource.id
    short = resource.short_id
    logger.info("[%s] parsing started type=%s", short, resource.resource_type)
    t0 = time.monotonic()

    try:
        db.transition(rid, ResourceStatus.RECEIVED, ResourceStatus.PARSING)
    except RuntimeError:
        logger.warning("[%s] CAS race — already claimed by another worker", short)
        return

    try:
        result = await parse(resource, settings.kb_root)
    except ParseError as e:
        elapsed = time.monotonic() - t0
        logger.error("[%s] parse error after %.1fs: %s", short, elapsed, e)
        db.transition(rid, ResourceStatus.PARSING, ResourceStatus.FAILED,
                      error_message=str(e))
        await notify_terminal(db, rid)
        return
    except TransientParseError as e:
        elapsed = time.monotonic() - t0
        logger.warning("[%s] transient parse error after %.1fs: %s — scheduling retry",
                       short, elapsed, e)
        ok = db.schedule_retry(rid, ResourceStatus.RECEIVED, str(e),
                               max_retries=settings.retries_max,
                               backoff_base_seconds=settings.retry_backoff_base_seconds)
        if not ok:
            logger.error("[%s] parse retries exhausted", short)
            db.transition(rid, ResourceStatus.PARSING, ResourceStatus.FAILED,
                          error_message=f"retries exhausted: {e}")
            await notify_terminal(db, rid)
        return
    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.exception("[%s] unexpected parse error after %.1fs", short, elapsed)
        ok = db.schedule_retry(rid, ResourceStatus.RECEIVED, str(e),
                               max_retries=settings.retries_max,
                               backoff_base_seconds=settings.retry_backoff_base_seconds)
        if not ok:
            logger.error("[%s] parse retries exhausted after unexpected error", short)
            db.transition(rid, ResourceStatus.PARSING, ResourceStatus.FAILED,
                          error_message=f"retries exhausted: {e}")
            await notify_terminal(db, rid)
        return

    elapsed = time.monotonic() - t0
    logger.info("[%s] parsed in %.1fs  title=%r  chars=%d  parser=%s",
                short, elapsed, result.title, result.char_count, result.parser_id)

    db.transition(rid, ResourceStatus.PARSING, ResourceStatus.PARSED,
                  parsed_text_path=result.parsed_path,
                  content_title=result.title)
    db.execute(
        "INSERT INTO events (resource_id, event_type, payload) VALUES (?, 'parsed', ?)",
        (rid, json.dumps({"title": result.title, "char_count": result.char_count,
                          "parser": result.parser_id})),
    )
    db.conn.commit()


async def handle_gate(db: Database, resource: Resource) -> None:
    rid = resource.id
    short = resource.short_id
    logger.info("[%s] gating started type=%s", short, resource.resource_type)
    t0 = time.monotonic()

    try:
        db.transition(rid, ResourceStatus.PARSED, ResourceStatus.GATING)
    except RuntimeError:
        logger.warning("[%s] CAS race — already claimed by another worker", short)
        return

    parsed_path = settings.kb_root / (resource.parsed_text_path or "")
    if not parsed_path.exists():
        logger.error("[%s] parsed file not found: %s", short, resource.parsed_text_path)
        db.transition(rid, ResourceStatus.GATING, ResourceStatus.FAILED,
                      error_message=f"parsed file not found: {resource.parsed_text_path}")
        await notify_terminal(db, rid)
        return

    body = parsed_path.read_text(encoding="utf-8")
    purpose_path = settings.kb_root / "purpose.md"
    purpose_md = purpose_path.read_text(encoding="utf-8") if purpose_path.exists() else ""

    source = resource.source_url or resource.original_file_path or "(inline text)"
    title = resource.content_title or ""

    logger.debug("[%s] gate input: source=%r  body_chars=%d  purpose_chars=%d",
                 short, source, len(body), len(purpose_md))

    try:
        gate_result, skipped = await gate_with_retries(
            resource.resource_type, source, title, body, purpose_md,
            max_retries=settings.retries_max,
        )
    except Exception as e:
        logger.error("[%s] gate infrastructure failure: %s", short, e)
        ok = db.schedule_retry(rid, ResourceStatus.PARSED, str(e),
                               max_retries=settings.retries_max,
                               backoff_base_seconds=settings.retry_backoff_base_seconds)
        if not ok:
            logger.warning("[%s] gate retries exhausted — default-accept", short)
            db.transition(rid, ResourceStatus.GATING, ResourceStatus.APPROVED,
                          quality_gate_skipped=True,
                          quality_score=65,
                          quality_rationale=f"gate infrastructure failure: {e}",
                          quality_topics=json.dumps([]))
        return

    elapsed = time.monotonic() - t0

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
        logger.info("[%s] gate accepted  score=%d  topics=%s  elapsed=%.1fs  %s",
                    short, gate_result.score, gate_result.topics, elapsed,
                    "(skipped)" if skipped else "")
        db.transition(rid, ResourceStatus.GATING, ResourceStatus.APPROVED,
                      quality_score=gate_result.score,
                      quality_rationale=gate_result.rationale,
                      quality_topics=json.dumps(gate_result.topics),
                      quality_gate_skipped=skipped)
    else:
        logger.info("[%s] gate rejected  score=%d  rationale=%r  elapsed=%.1fs",
                    short, gate_result.score, gate_result.rationale, elapsed)
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
        await notify_terminal(db, rid)


async def run_resource_worker() -> None:
    logger.info("starting resource worker  poll_interval=%ds", settings.poll_interval_seconds)
    db = Database(settings.state_db)
    try:
        db.connect()
        db.run_migrations()
        logger.info("database connected and migrated")
        await poll_loop(db, handle_next, settings.poll_interval_seconds,
                        name="resource-worker")
    finally:
        db.close()
        logger.info("database connection closed")
