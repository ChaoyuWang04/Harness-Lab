from __future__ import annotations

import time
from collections.abc import Callable

from redis import Redis
from rq import Queue

from app.config import settings
from app.chaos.hooks import crash_after_publish_once
from app.db import SessionLocal, engine
from app.outbox import dispatch_batch
from app.telemetry import initialize_observability, register_database_gauges


def main() -> None:
    initialize_observability("harness-dispatcher", engine=engine)
    register_database_gauges(SessionLocal)
    queue = Queue("runs", connection=Redis.from_url(settings.redis_url))
    post_publish_hook: Callable[[str, int], object] | None = None
    if settings.chaos_dispatcher_crash_after_publish:
        post_publish_hook = lambda _run_id, _outbox_id: crash_after_publish_once(
            settings.chaos_dispatcher_marker
        )
    while True:
        dispatch_batch(SessionLocal, queue, post_publish_hook=post_publish_hook)
        time.sleep(1)


if __name__ == "__main__":
    main()
