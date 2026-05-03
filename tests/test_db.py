"""Tests for the SQLite database layer."""

import pytest

from app.db import Database
from app.enums import ResourceStatus


def test_schema_applied(db: Database):
    """Verify tables and indexes exist after migrations."""
    tables = db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    table_names = {r["name"] for r in tables}
    assert "resources" in table_names
    assert "events" in table_names


def test_insert_resource(db: Database):
    rid = "00000000-0000-4000-a000-000000000001"
    db.insert_resource(
        id=rid,
        resource_type="web",
        source_url="https://example.com/test",
        telegram_chat_id=123,
        telegram_message_id=456,
        telegram_user_id=789,
    )
    row = db.fetchone("SELECT * FROM resources WHERE id = ?", (rid,))
    assert row is not None
    assert row["resource_type"] == "web"
    assert row["status"] == ResourceStatus.RECEIVED
    assert row["source_url"] == "https://example.com/test"


def test_transition_success(db: Database):
    rid = "00000000-0000-4000-a000-000000000002"
    db.insert_resource(id=rid, resource_type="web")

    db.transition(rid, ResourceStatus.RECEIVED, ResourceStatus.PARSING)

    row = db.fetchone("SELECT * FROM resources WHERE id = ?", (rid,))
    assert row["status"] == ResourceStatus.PARSING

    # Verify event was written
    events = db.fetchall("SELECT * FROM events WHERE resource_id = ?", (rid,))
    assert len(events) >= 1


def test_transition_cas_guard(db: Database):
    """Transition must fail if the row is not in the expected status."""
    rid = "00000000-0000-4000-a000-000000000003"
    db.insert_resource(id=rid, resource_type="web")

    # Try to transition from wrong status
    with pytest.raises(RuntimeError, match="CAS failed"):
        db.transition(rid, ResourceStatus.PARSED, ResourceStatus.APPROVED)


def test_schedule_retry(db: Database):
    rid = "00000000-0000-4000-a000-000000000004"
    db.insert_resource(id=rid, resource_type="web")

    ok = db.schedule_retry(rid, ResourceStatus.RECEIVED, "test error")
    assert ok is True

    row = db.fetchone("SELECT * FROM resources WHERE id = ?", (rid,))
    assert row["retry_count"] == 1
    assert row["status"] == ResourceStatus.RECEIVED
    assert row["next_attempt_at"] is not None
    assert "test error" in (row["error_message"] or "")


def test_schedule_retry_exhausted(db: Database):
    rid = "00000000-0000-4000-a000-000000000005"
    db.insert_resource(id=rid, resource_type="web")

    # Set retry_count to max
    db.execute("UPDATE resources SET retry_count = 3 WHERE id = ?", (rid,))
    db.conn.commit()

    ok = db.schedule_retry(rid, ResourceStatus.RECEIVED, "error", max_retries=3)
    assert ok is False


def test_poll_resources(db: Database):
    rid1 = "00000000-0000-4000-a000-000000000006"
    rid2 = "00000000-0000-4000-a000-000000000007"
    rid3 = "00000000-0000-4000-a000-000000000008"

    db.insert_resource(id=rid1, resource_type="web")
    db.insert_resource(id=rid2, resource_type="pdf")
    db.insert_resource(id=rid3, resource_type="text")

    # First one should be polled
    rows = db.poll_resources([ResourceStatus.RECEIVED], limit=1)
    assert len(rows) == 1
    assert rows[0]["id"] == rid1

    # Two available
    rows = db.poll_resources([ResourceStatus.RECEIVED], limit=5)
    assert len(rows) == 3


def test_poll_skips_future_attempts(db: Database):
    rid = "00000000-0000-4000-a000-000000000009"
    db.insert_resource(id=rid, resource_type="web")
    db.execute(
        "UPDATE resources SET next_attempt_at = datetime('now', '+1 hour') WHERE id = ?",
        (rid,),
    )
    db.conn.commit()

    rows = db.poll_resources([ResourceStatus.RECEIVED])
    assert len(rows) == 0


def test_reset_stuck_rows(db: Database):
    rid = "00000000-0000-4000-a000-000000000010"
    db.insert_resource(id=rid, resource_type="web")
    db.execute(
        "UPDATE resources SET status = 'parsing', updated_at = datetime('now', '-1 hour') "
        "WHERE id = ?",
        (rid,),
    )
    db.conn.commit()

    count = db.reset_stuck_rows(stuck_minutes=30)
    assert count == 1

    row = db.fetchone("SELECT * FROM resources WHERE id = ?", (rid,))
    assert row["status"] == ResourceStatus.RECEIVED
    assert "sweeper:stuck" in (row["error_message"] or "")
