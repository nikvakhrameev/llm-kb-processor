"""Entry point: python -m app.worker_ingest"""

import asyncio

from app.workers.ingest import run_ingest_worker

if __name__ == "__main__":
    asyncio.run(run_ingest_worker())
