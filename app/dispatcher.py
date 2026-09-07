from __future__ import annotations

import time

from redis import Redis
from rq import Queue

from app.config import settings
from app.db import SessionLocal, engine
from app.outbox import dispatch_batch
from app.telemetry import initialize_observability, register_database_gauges


def main() -> None:
    initialize_observability("harness-dispatcher", engine=engine)
    register_database_gauges(SessionLocal)
    queue = Queue("runs", connection=Redis.from_url(settings.redis_url))
    while True:
        dispatch_batch(SessionLocal, queue)
        time.sleep(1)


if __name__ == "__main__":
    main()
