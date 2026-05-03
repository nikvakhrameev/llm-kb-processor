"""APScheduler process — cron jobs (lint, synthesis) and sweeper."""

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from app.cron.lint import enqueue_lint
from app.cron.sweeper import run_sweeper
from app.cron.synthesis import enqueue_synthesis
from app.db import Database
from app.settings import settings

# Global database handle for the scheduler process
_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        raise RuntimeError("Scheduler not initialized. Call init_scheduler first.")
    return _db


def init_scheduler() -> AsyncIOScheduler:
    """Create and configure the APScheduler with SQLAlchemy job store."""
    global _db
    _db = Database(settings.state_db)
    _db.connect()
    _db.run_migrations()

    # Enable WAL on the APScheduler's SQLAlchemy engine
    engine_url = f"sqlite:///{settings.state_db}"
    engine = create_engine(
        engine_url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_wal(dbapi_connection, connection_record):
        dbapi_connection.execute("PRAGMA journal_mode=WAL")

    scheduler = AsyncIOScheduler()
    scheduler.add_jobstore("sqlalchemy", engine=engine)

    # Daily lint
    scheduler.add_job(
        lambda: asyncio.create_task(enqueue_lint(_db)),
        CronTrigger(hour=settings.lint_hour, minute=settings.lint_minute,
                     timezone=settings.tz),
        id="daily_lint",
        name="Daily lint",
        replace_existing=True,
    )

    # Weekly synthesis
    scheduler.add_job(
        lambda: asyncio.create_task(enqueue_synthesis(_db)),
        CronTrigger(day_of_week=settings.synthesis_day,
                     hour=settings.synthesis_hour,
                     minute=settings.synthesis_minute,
                     timezone=settings.tz),
        id="weekly_synthesis",
        name="Weekly synthesis",
        replace_existing=True,
    )

    # Sweeper (every 5 minutes)
    scheduler.add_job(
        lambda: asyncio.create_task(run_sweeper(_db)),
        CronTrigger(minute="*/5"),
        id="sweeper",
        name="Stuck-row sweeper",
        replace_existing=True,
    )

    return scheduler


async def run_scheduler() -> None:
    """Entry point for the scheduler process."""
    scheduler = init_scheduler()
    scheduler.start()
    print("[scheduler] Started with 3 jobs: daily_lint, weekly_synthesis, sweeper")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, asyncio.CancelledError):
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(run_scheduler())
