"""Entry point: python -m app.worker_resources"""

import asyncio

from app.workers.resources import run_resource_worker

if __name__ == "__main__":
    asyncio.run(run_resource_worker())
