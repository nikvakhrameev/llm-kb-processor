"""Daily lint cron job — enqueues a synthetic resource row."""

from app.db import Database
from app.enums import ResourceType, ResourceStatus
from app.utils import uuid_str


async def enqueue_lint(db: Database) -> str:
    """Insert a _lint row so the ingest worker picks it up."""
    rid = uuid_str()
    db.insert_resource(
        id=rid,
        resource_type=ResourceType.LINT,
        status=ResourceStatus.APPROVED,
    )
    print(f"[cron] Enqueued lint job: {rid[:8]}")
    return rid
