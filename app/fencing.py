from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AgentRun


@dataclass(frozen=True, slots=True)
class WorkerFence:
    owner: str
    attempt: int


def lock_current_run(session: Session, run_id: str, fence: WorkerFence) -> AgentRun | None:
    run = session.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
    if run is None:
        return None
    database_now = session.scalar(select(func.now()))
    if not (
        run.status == "running"
        and run.lease_owner == fence.owner
        and run.attempt == fence.attempt
        and run.lease_expires_at is not None
        and run.lease_expires_at > database_now
    ):
        return None
    return run
