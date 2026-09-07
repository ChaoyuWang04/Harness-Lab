from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))


def test_carrier_preserves_trace_and_parent_identity() -> None:
    from app.telemetry import inject_trace_context, span_from_carrier

    tracer = TracerProvider().get_tracer("trace-test")
    with tracer.start_as_current_span("api") as parent:
        carrier = inject_trace_context()
        parent_context = parent.get_span_context()
    with span_from_carrier("dispatcher", carrier, tracer=tracer) as child:
        child_context = child.get_span_context()

    assert carrier["traceparent"].startswith("00-")
    assert child_context.trace_id == parent_context.trace_id
    assert child.parent is not None
    assert child.parent.span_id == parent_context.span_id


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires Compose PostgreSQL")
def test_create_and_dispatch_persist_trace_context_in_both_async_hops() -> None:
    from app.models import AgentRun, IdempotencyKey, OutboxJob, RunEvent
    from app.outbox import dispatch_batch
    from app.runs import create_run

    engine = create_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)
    sessions = sessionmaker(engine, expire_on_commit=False)

    class Queue:
        meta = None

        def enqueue_call(self, _func, *, args, job_id, unique, meta):
            self.meta = meta
            return object()

    queue = Queue()
    try:
        with sessions.begin() as session:
            session.execute(delete(IdempotencyKey))
            session.execute(delete(OutboxJob))
            session.execute(delete(RunEvent))
            session.execute(delete(AgentRun))
            run = create_run(
                session,
                {"prompt": "trace"},
                trace_context={"traceparent": "00-11111111111111111111111111111111-2222222222222222-01"},
            )
        with sessions() as session:
            payload = session.scalar(select(OutboxJob.payload).where(OutboxJob.payload["run_id"].astext == run.id))
        assert payload["trace_context"]["traceparent"].endswith("2222222222222222-01")

        assert dispatch_batch(sessions, queue) == 1
        assert queue.meta is not None
        assert queue.meta["trace_context"]["traceparent"].startswith("00-11111111111111111111111111111111-")
    finally:
        engine.dispose()
