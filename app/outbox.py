from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from typing import Protocol

from rq.exceptions import DuplicateJobError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.events import append_event
from app.models import OutboxJob
from app.telemetry import inject_trace_context, span_from_carrier


class QueueLike(Protocol):
    def enqueue_call(
        self,
        func: str,
        *,
        args: list[str],
        job_id: str,
        unique: bool,
        meta: dict[str, object],
    ) -> object: ...


def dispatch_batch(
    session_factory: sessionmaker[Session],
    queue: QueueLike,
    *,
    limit: int = 100,
    now: datetime | None = None,
    post_publish_hook: Callable[[str, int], object] | None = None,
) -> int:
    checked_at = now or datetime.now(timezone.utc)
    dispatched = 0
    with session_factory.begin() as session:
        jobs = session.scalars(
            select(OutboxJob)
            .where(
                OutboxJob.status == "pending",
                OutboxJob.next_attempt_at <= checked_at,
            )
            .order_by(OutboxJob.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for job in jobs:
            run_id = str(job.payload["run_id"])
            delivery_id = f"{run_id}_outbox_{job.id}"
            try:
                trace_context = job.payload.get("trace_context", {})
                with span_from_carrier(
                    "harness.dispatch",
                    trace_context if isinstance(trace_context, dict) else {},
                    attributes={"run.id": run_id, "outbox.id": job.id},
                ):
                    queue.enqueue_call(
                        "app.jobs.execute_run",
                        args=[run_id],
                        job_id=delivery_id,
                        unique=True,
                        meta={"trace_context": inject_trace_context(), "run_id": run_id},
                    )
            except DuplicateJobError:
                pass
            except Exception as exc:
                job.attempts += 1
                delay_seconds = min(60, 2 ** (job.attempts - 1))
                job.next_attempt_at = checked_at + timedelta(seconds=delay_seconds)
                job.last_error = type(exc).__name__
                continue

            if post_publish_hook is not None:
                post_publish_hook(run_id, job.id)
            job.status = "dispatched"
            job.last_error = None
            append_event(session, run_id, "run.enqueued", {"outbox_job_id": job.id})
            dispatched += 1
    return dispatched
