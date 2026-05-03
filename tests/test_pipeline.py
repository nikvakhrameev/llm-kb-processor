"""Integration tests for the state machine pipeline."""

import json

import pytest

from app.db import Database
from app.enums import ResourceStatus


def test_full_state_machine_lifecycle(db: Database):
    """Walk a resource through the full lifecycle: received -> done."""
    rid = "00000000-0000-4000-a000-000000000201"

    # Insert
    db.insert_resource(id=rid, resource_type="web",
                       source_url="https://example.com/test")
    row = db.fetchone("SELECT * FROM resources WHERE id = ?", (rid,))
    assert row["status"] == ResourceStatus.RECEIVED

    # received -> parsing
    db.transition(rid, ResourceStatus.RECEIVED, ResourceStatus.PARSING)
    # parsing -> parsed
    db.transition(rid, ResourceStatus.PARSING, ResourceStatus.PARSED,
                  parsed_text_path="raw/parsed/web/test.md",
                  content_title="Test Article")

    # parsed -> gating
    db.transition(rid, ResourceStatus.PARSED, ResourceStatus.GATING)
    # gating -> approved
    db.transition(rid, ResourceStatus.GATING, ResourceStatus.APPROVED,
                  quality_score=82,
                  quality_rationale="Useful content",
                  quality_topics=json.dumps(["test"]))

    # approved -> ingesting
    db.transition(rid, ResourceStatus.APPROVED, ResourceStatus.INGESTING)
    # ingesting -> done
    db.transition(rid, ResourceStatus.INGESTING, ResourceStatus.DONE,
                  ingest_commit_sha="abc1234",
                  ingest_summary=json.dumps({"pages_created": ["wiki/sources/test.md"],
                                             "pages_updated": [], "warnings": []}))

    row = db.fetchone("SELECT * FROM resources WHERE id = ?", (rid,))
    assert row["status"] == ResourceStatus.DONE
    assert row["completed_at"] is not None

    # Verify audit events
    events = db.fetchall("SELECT * FROM events WHERE resource_id = ? ORDER BY id", (rid,))
    status_change_events = [e for e in events if e["event_type"] == "status_change"]
    # Should have at least 7 transitions (receive is implicit on insert, but we
    # only log explicit transitions)
    assert len(status_change_events) >= 6


def test_rejected_path(db: Database):
    """Test the rejected terminal path."""
    rid = "00000000-0000-4000-a000-000000000202"
    db.insert_resource(id=rid, resource_type="web",
                       source_url="https://example.com/bad")

    db.transition(rid, ResourceStatus.RECEIVED, ResourceStatus.PARSING)
    db.transition(rid, ResourceStatus.PARSING, ResourceStatus.PARSED,
                  parsed_text_path="raw/parsed/web/bad.md",
                  content_title="Cookie Banner")
    db.transition(rid, ResourceStatus.PARSED, ResourceStatus.GATING)
    db.transition(rid, ResourceStatus.GATING, ResourceStatus.REJECTED,
                  quality_score=22,
                  quality_rationale="Just a cookie banner")

    row = db.fetchone("SELECT * FROM resources WHERE id = ?", (rid,))
    assert row["status"] == ResourceStatus.REJECTED


def test_failed_path(db: Database):
    """Test the failed terminal path after retries exhausted."""
    rid = "00000000-0000-4000-a000-000000000203"
    db.insert_resource(id=rid, resource_type="web",
                       source_url="https://example.com/fail")

    # Simulate parsing failure after retries
    db.transition(rid, ResourceStatus.RECEIVED, ResourceStatus.PARSING)
    db.transition(rid, ResourceStatus.PARSING, ResourceStatus.FAILED,
                  error_message="404 not found")

    row = db.fetchone("SELECT * FROM resources WHERE id = ?", (rid,))
    assert row["status"] == ResourceStatus.FAILED
    assert row["completed_at"] is not None


def test_cannot_transition_from_terminal(db: Database):
    """Once done/failed/rejected, transitions should be blocked by CAS guard."""
    rid = "00000000-0000-4000-a000-000000000204"
    db.insert_resource(id=rid, resource_type="web")
    db.transition(rid, ResourceStatus.RECEIVED, ResourceStatus.PARSING)
    db.transition(rid, ResourceStatus.PARSING, ResourceStatus.FAILED,
                  error_message="done")

    with pytest.raises(RuntimeError, match="CAS failed"):
        db.transition(rid, ResourceStatus.RECEIVED, ResourceStatus.PARSING)
