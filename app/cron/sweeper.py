"""Sweeper — resets resources stuck in in-flight states for too long.

Runs every 5 minutes. If a worker crashes mid-flight (parsing, gating, ingesting),
the sweeper detects the stale updated_at and resets the row to its previous
pickable status with an incremented retry_count.
"""

from app.db import Database
from app.settings import settings


async def run_sweeper(db: Database) -> int:
    """Reset stuck rows. Returns the number of rows reset."""
    count = db.reset_stuck_rows(stuck_minutes=settings.sweeper_stuck_minutes)
    if count > 0:
        print(f"[sweeper] Reset {count} stuck row(s)")
    return count
