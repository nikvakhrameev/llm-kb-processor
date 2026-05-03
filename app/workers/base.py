"""Shared base for worker poll loops."""

import asyncio
from typing import Awaitable, Callable

from app.db import Database


async def poll_loop(
    db: Database,
    handler: Callable[..., Awaitable[None]],
    interval_seconds: int,
    *,
    name: str = "worker",
) -> None:
    """Run a polling loop: poll -> handle -> sleep.

    The handler receives the database and a resource row.
    """
    print(f"[{name}] Starting poll loop (interval={interval_seconds}s)")
    while True:
        try:
            await handler(db)
        except Exception as exc:
            print(f"[{name}] Poll loop error: {exc}")
        await asyncio.sleep(interval_seconds)
