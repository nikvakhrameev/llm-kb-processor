"""Tests for cron job functions."""

from app.cron.lint import enqueue_lint
from app.cron.synthesis import enqueue_synthesis
from app.cron.sweeper import run_sweeper
from app.db import Database
from app.enums import ResourceStatus, ResourceType


def test_enqueue_lint(db: Database):
    """Enqueuing a lint job creates an approved _lint row."""
    rid = None
    import asyncio
    rid = asyncio.run(enqueue_lint(db))

    row = db.fetchone("SELECT * FROM resources WHERE id = ?", (rid,))
    assert row is not None
    assert row["resource_type"] == ResourceType.LINT
    assert row["status"] == ResourceStatus.APPROVED


def test_enqueue_synthesis(db: Database):
    """Enqueuing a synthesis job creates an approved _synthesis_weekly row."""
    import asyncio
    rid = asyncio.run(enqueue_synthesis(db))

    row = db.fetchone("SELECT * FROM resources WHERE id = ?", (rid,))
    assert row is not None
    assert row["resource_type"] == ResourceType.SYNTHESIS_WEEKLY
    assert row["status"] == ResourceStatus.APPROVED


def test_sweeper_no_stuck_rows(db: Database):
    """Sweeper with no stuck rows returns 0."""
    import asyncio
    count = asyncio.run(run_sweeper(db))
    assert count == 0


def test_sweeper_resets_stuck(db: Database):
    """Sweeper resets rows stuck in parsing for too long."""
    rid = "00000000-0000-4000-a000-000000000401"
    db.insert_resource(id=rid, resource_type="web",
                       telegram_user_id=123, telegram_chat_id=123, telegram_message_id=999)
    # Manually set to stuck state
    db.execute(
        "UPDATE resources SET status = 'parsing', updated_at = datetime('now', '-1 hour') "
        "WHERE id = ?",
        (rid,),
    )
    db.conn.commit()

    import asyncio
    count = asyncio.run(run_sweeper(db))
    assert count == 1

    row = db.fetchone("SELECT * FROM resources WHERE id = ?", (rid,))
    assert row["status"] == ResourceStatus.RECEIVED
    assert "sweeper:stuck" in (row["error_message"] or "")
    assert row["retry_count"] == 1
