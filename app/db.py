from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def make_engine(url: str | None = None) -> Engine:
    return create_engine(
        url or settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )


def warm_engine_pool(bound_engine: Engine, connections: int) -> int:
    opened = []
    try:
        for _ in range(connections):
            opened.append(bound_engine.connect())
    finally:
        for connection in reversed(opened):
            connection.close()
    return len(opened)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
