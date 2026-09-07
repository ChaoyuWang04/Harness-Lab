from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import AgentRun, OutboxJob


RUN_STATUSES = ("queued", "running", "completed", "failed", "cancelled")


def collect_debug_metrics(session: Session) -> dict[str, object]:
    pending = session.scalar(
        select(func.count()).select_from(OutboxJob).where(OutboxJob.status == "pending")
    )
    counts = {
        status: int(
            session.scalar(
                select(func.count()).select_from(AgentRun).where(AgentRun.status == status)
            )
            or 0
        )
        for status in RUN_STATUSES
    }
    return {"outbox_pending": int(pending or 0), "runs": counts}


def build_metrics_router(session_factory: sessionmaker[Session]) -> APIRouter:
    router = APIRouter(prefix="/metrics", tags=["internal"])

    def get_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    @router.get("/debug")
    def metrics_debug(session: Session = Depends(get_session)) -> dict[str, object]:
        return collect_debug_metrics(session)

    return router
