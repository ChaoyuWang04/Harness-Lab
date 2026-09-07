from __future__ import annotations

from redis import Redis
from rq import Queue, Worker

from app.config import settings


def main() -> None:
    connection = Redis.from_url(settings.redis_url)
    Worker([Queue("runs", connection=connection)], connection=connection).work()


if __name__ == "__main__":
    main()
