from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AgentRun, IdempotencyKey, OutboxJob, RunEvent


def new_run_id() -> str:
    timestamp_ms = int(time.time() * 1000)
    return f"run_{timestamp_ms:013d}{uuid.uuid4().hex[:16]}"


def create_run(
    session: Session,
    input_json: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    trace_context: dict[str, str] | None = None,
) -> AgentRun:
    if idempotency_key:
        session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(idempotency_key, 0))))
        existing_id = session.scalar(
            select(IdempotencyKey.run_id).where(IdempotencyKey.key == idempotency_key)
        )
        if existing_id:
            existing = session.get(AgentRun, existing_id)
            if existing is None:
                raise RuntimeError("idempotency key references a missing run")
            return existing

    run = AgentRun(id=new_run_id(), status="queued", input_json=input_json)
    payload: dict[str, Any] = {"run_id": run.id}
    if trace_context:
        payload["trace_context"] = trace_context
    records = [
        run,
        RunEvent(run_id=run.id, sequence=1, type="run.created", payload={}),
        OutboxJob(task="execute_run", payload=payload),
    ]
    if idempotency_key:
        records.append(IdempotencyKey(key=idempotency_key, run_id=run.id))
    session.add_all(records)
    session.flush()
    return run
