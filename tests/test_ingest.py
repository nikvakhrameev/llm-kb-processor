"""Tests for the ingest worker — mocking claude-agent-sdk."""

import json

import pytest

from app.db import Database
from app.enums import ResourceStatus
from app.models import Resource


def _make_approved(db: Database, rid: str, resource_type: str = "web",
                    parsed_path: str = "raw/parsed/web/test.md",
                    title: str = "Test Article") -> None:
    """Insert a resource and fast-forward it to approved status."""
    db.insert_resource(
        id=rid, resource_type=resource_type,
        source_url="https://example.com/test",
        telegram_user_id=123, telegram_chat_id=123, telegram_message_id=456,
    )
    db.transition(rid, ResourceStatus.RECEIVED, ResourceStatus.PARSING)
    db.transition(rid, ResourceStatus.PARSING, ResourceStatus.PARSED,
                  parsed_text_path=parsed_path, content_title=title)
    db.transition(rid, ResourceStatus.PARSED, ResourceStatus.GATING)
    db.transition(rid, ResourceStatus.GATING, ResourceStatus.APPROVED,
                  quality_score=82,
                  quality_rationale="Useful",
                  quality_topics=json.dumps(["test"]))


def test_approved_resource_is_polled(db: Database):
    """Approved resources with no next_attempt_at should be returned."""
    rid = "00000000-0000-4000-a000-000000000301"
    _make_approved(db, rid)

    rows = db.poll_resources([ResourceStatus.APPROVED])
    assert len(rows) >= 1
    assert rows[0]["id"] == rid


def test_approved_resource_skips_future_attempts(db: Database):
    """Resources with future next_attempt_at should be skipped."""
    rid = "00000000-0000-4000-a000-000000000302"
    _make_approved(db, rid)
    db.execute(
        "UPDATE resources SET next_attempt_at = datetime('now', '+1 hour') WHERE id = ?",
        (rid,),
    )
    db.conn.commit()

    rows = db.poll_resources([ResourceStatus.APPROVED])
    assert len(rows) == 0


def test_ingest_transition_cas(db: Database):
    """Transition from approved to ingesting requires CAS guard."""
    rid = "00000000-0000-4000-a000-000000000303"
    _make_approved(db, rid)

    db.transition(rid, ResourceStatus.APPROVED, ResourceStatus.INGESTING)
    row = db.fetchone("SELECT status FROM resources WHERE id = ?", (rid,))
    assert row["status"] == ResourceStatus.INGESTING

    # Second transition from approved should fail (status is now ingesting)
    with pytest.raises(RuntimeError, match="CAS failed"):
        db.transition(rid, ResourceStatus.APPROVED, ResourceStatus.INGESTING)


def test_ingest_to_done(db: Database):
    """Successful ingest: ingesting -> done."""
    rid = "00000000-0000-4000-a000-000000000304"
    _make_approved(db, rid)

    db.transition(rid, ResourceStatus.APPROVED, ResourceStatus.INGESTING)
    db.transition(rid, ResourceStatus.INGESTING, ResourceStatus.DONE,
                  ingest_commit_sha="abc1234",
                  ingest_summary=json.dumps({
                      "pages_created": ["wiki/sources/test.md"],
                      "pages_updated": ["wiki/entities/Foo.md"],
                      "warnings": [],
                  }))

    row = db.fetchone("SELECT * FROM resources WHERE id = ?", (rid,))
    assert row["status"] == ResourceStatus.DONE
    assert row["ingest_commit_sha"] == "abc1234"
    assert row["completed_at"] is not None


def test_ingest_retry_on_failure(db: Database):
    """Failed ingest should schedule a retry back to approved."""
    rid = "00000000-0000-4000-a000-000000000305"
    _make_approved(db, rid)

    db.transition(rid, ResourceStatus.APPROVED, ResourceStatus.INGESTING)

    # Simulate failure: schedule_retry rolls back to approved
    ok = db.schedule_retry(rid, ResourceStatus.APPROVED, "agent timeout",
                           max_retries=3)
    assert ok is True

    row = db.fetchone("SELECT * FROM resources WHERE id = ?", (rid,))
    assert row["status"] == ResourceStatus.APPROVED
    assert row["retry_count"] == 1
    assert row["next_attempt_at"] is not None
