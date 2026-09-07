from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.fencing import WorkerFence, lock_current_run
from app.models import AgentRun, RunEvent


def append_event(
    session: Session,
    run_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    fence: WorkerFence | None = None,
) -> RunEvent:
    run = (
        lock_current_run(session, run_id, fence)
        if fence is not None
        else session.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
    )
    if run is None:
        if fence is not None:
            raise PermissionError("worker lease is no longer current")
        raise LookupError(f"run not found: {run_id}")
    last_sequence = session.scalar(
        select(func.coalesce(func.max(RunEvent.sequence), 0)).where(RunEvent.run_id == run_id)
    )
    event = RunEvent(
        run_id=run_id,
        sequence=int(last_sequence or 0) + 1,
        type=event_type,
        payload=payload or {},
    )
    session.add(event)
    session.flush()
    return event
