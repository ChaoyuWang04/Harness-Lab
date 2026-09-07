from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db import SessionLocal, engine
from app.events import append_event
from app.models import AgentRun, OutboxJob
from app.telemetry import get_harness_metrics, initialize_observability


def sweep_once(
    session_factory: sessionmaker[Session],
    *,
    max_attempts: int,
    limit: int = 100,
) -> int:
    transitioned = 0
    outcomes: list[str] = []
    with session_factory.begin() as session:
        runs = session.scalars(
            select(AgentRun)
            .where(
                AgentRun.status == "running",
                AgentRun.lease_expires_at < func.now() - timedelta(seconds=5),
            )
            .order_by(AgentRun.updated_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for run in runs:
            if run.attempt >= max_attempts:
                append_event(session, run.id, "run.failed", {"error_code": "MAX_RETRY"})
                run.status = "failed"
                run.error_code = "MAX_RETRY"
                outcomes.append("max_retry")
            else:
                append_event(
                    session,
                    run.id,
                    "run.requeued_by_sweeper",
                    {"expired_owner": run.lease_owner, "attempt": run.attempt},
                )
                run.status = "queued"
                prior_payload = session.scalar(
                    select(OutboxJob.payload)
                    .where(OutboxJob.payload["run_id"].astext == run.id)
                    .order_by(OutboxJob.id.desc())
                    .limit(1)
                )
                payload = {"run_id": run.id}
                if isinstance(prior_payload, dict) and isinstance(
                    prior_payload.get("trace_context"), dict
                ):
                    payload["trace_context"] = prior_payload["trace_context"]
                session.add(OutboxJob(task="execute_run", payload=payload))
                outcomes.append("requeued")
            run.lease_owner = None
            run.lease_expires_at = None
            run.updated_at = func.now()
            transitioned += 1
    for outcome in outcomes:
        get_harness_metrics().record_sweep(outcome)
    return transitioned


def main() -> None:
    initialize_observability("harness-sweeper", engine=engine)
    while True:
        sweep_once(SessionLocal, max_attempts=settings.max_attempts)
        time.sleep(10)


if __name__ == "__main__":
    main()
