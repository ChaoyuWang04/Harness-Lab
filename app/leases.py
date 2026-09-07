from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.events import append_event
from app.fencing import WorkerFence, lock_current_run
from app.models import AgentRun


def claim_run(
    session_factory: sessionmaker[Session],
    run_id: str,
    owner: str,
    *,
    lease_seconds: int,
) -> WorkerFence | None:
    with session_factory.begin() as session:
        attempt = session.scalar(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(("queued", "running")),
                or_(AgentRun.lease_expires_at.is_(None), AgentRun.lease_expires_at < func.now()),
            )
            .values(
                status="running",
                lease_owner=owner,
                lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
                attempt=AgentRun.attempt + 1,
                updated_at=func.now(),
            )
            .returning(AgentRun.attempt)
        )
    return WorkerFence(owner, attempt) if attempt is not None else None


def renew_lease(
    session_factory: sessionmaker[Session],
    run_id: str,
    fence: WorkerFence,
    *,
    lease_seconds: int,
) -> bool:
    with session_factory.begin() as session:
        renewed = session.scalar(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == "running",
                AgentRun.lease_owner == fence.owner,
                AgentRun.attempt == fence.attempt,
                AgentRun.lease_expires_at > func.now(),
            )
            .values(
                lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
                updated_at=func.now(),
            )
            .returning(AgentRun.id)
        )
    return renewed is not None


def finish_run(
    session_factory: sessionmaker[Session],
    run_id: str,
    fence: WorkerFence,
    *,
    status: Literal["completed", "failed"],
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> bool:
    with session_factory.begin() as session:
        run = lock_current_run(session, run_id, fence)
        if run is None:
            return False
        append_event(
            session,
            run_id,
            f"run.{status}",
            result if status == "completed" else {"error_code": error_code},
            fence=fence,
        )
        run.status = status
        run.result_json = result
        run.error_code = error_code
        run.lease_owner = None
        run.lease_expires_at = None
        run.updated_at = func.now()
    return True
