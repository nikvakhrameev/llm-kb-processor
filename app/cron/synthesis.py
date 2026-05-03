"""Weekly synthesis cron job — enqueues a synthetic resource row."""

from app.db import Database
from app.enums import ResourceType, ResourceStatus
from app.utils import uuid_str


async def enqueue_synthesis(db: Database) -> str:
    """Insert a _synthesis_weekly row so the ingest worker picks it up."""
    rid = uuid_str()
    db.insert_resource(
        id=rid,
        resource_type=ResourceType.SYNTHESIS_WEEKLY,
        status=ResourceStatus.APPROVED,
    )
    print(f"[cron] Enqueued weekly synthesis: {rid[:8]}")
    return rid
