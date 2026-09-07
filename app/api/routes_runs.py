from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sse_starlette.sse import EventSourceResponse

from app.models import AgentRun, RunEvent
from app.runs import create_run
from app.telemetry import inject_trace_context


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class RunCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


class RunCreated(BaseModel):
    run_id: str
    status: str


class RunView(BaseModel):
    run_id: str
    status: str
    input: dict[str, Any]
    result: dict[str, Any] | None
    error_code: str | None
    attempt: int


def build_router(session_factory: sessionmaker[Session]) -> APIRouter:
    router = APIRouter(prefix="/runs", tags=["runs"])

    def get_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    @router.post("", response_model=RunCreated, status_code=status.HTTP_201_CREATED)
    def post_run(
        body: RunCreate,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        session: Session = Depends(get_session),
    ) -> RunCreated:
        started = time.perf_counter()
        tracer = trace.get_tracer("harness-lab.api")
        with tracer.start_as_current_span("harness.api_create_run") as span:
            with session.begin():
                run = create_run(
                    session,
                    {"prompt": body.prompt},
                    idempotency_key=idempotency_key,
                    trace_context=inject_trace_context(),
                )
            span.set_attribute("run.id", run.id)
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(json.dumps({"event": "run.created.http", "run_id": run.id, "latency_ms": round(elapsed_ms, 3)}))
        return RunCreated(run_id=run.id, status=run.status)

    @router.get("/{run_id}", response_model=RunView)
    def get_run(run_id: str, session: Session = Depends(get_session)) -> RunView:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return RunView(
            run_id=run.id,
            status=run.status,
            input=run.input_json,
            result=run.result_json,
            error_code=run.error_code,
            attempt=run.attempt,
        )

    @router.get("/{run_id}/events")
    async def get_events(
        run_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> EventSourceResponse:
        try:
            cursor = int(last_event_id or 0)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc

        with session_factory() as session:
            if session.get(AgentRun, run_id) is None:
                raise HTTPException(status_code=404, detail="run not found")

        async def stream():
            nonlocal cursor
            last_heartbeat = time.monotonic()
            while True:
                with session_factory() as session:
                    events = session.scalars(
                        select(RunEvent)
                        .where(RunEvent.run_id == run_id, RunEvent.sequence > cursor)
                        .order_by(RunEvent.sequence)
                    ).all()
                    run_status = session.scalar(select(AgentRun.status).where(AgentRun.id == run_id))
                    materialized = [
                        (event.sequence, event.type, event.payload) for event in events
                    ]

                for sequence, event_type, payload in materialized:
                    cursor = sequence
                    yield {
                        "id": str(sequence),
                        "event": event_type,
                        "data": json.dumps(payload, separators=(",", ":")),
                    }

                if run_status in TERMINAL_STATUSES:
                    return
                if await request.is_disconnected():
                    return
                now = time.monotonic()
                if now - last_heartbeat >= 15:
                    yield {"comment": "heartbeat"}
                    last_heartbeat = now
                await asyncio.sleep(0.5)

        return EventSourceResponse(stream())

    return router
