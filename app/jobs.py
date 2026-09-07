from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from collections.abc import Callable

from openai import APIStatusError, APITimeoutError, RateLimitError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.llm import ChatClient, OllamaClient
from app.agent.loop import BadOutput, run_agent
from app.agent.tools import ToolError, ToolResultUnknown
from app.config import settings
from app.db import SessionLocal
from app.events import append_event
from app.leases import claim_run, finish_run, renew_lease
from app.models import AgentRun
from app.telemetry import (
    capture_sentry_exception,
    get_harness_metrics,
    initialize_observability,
    span_from_carrier,
)


def classify_error(error: BaseException) -> str:
    if isinstance(error, RateLimitError):
        return "MODEL_429"
    if isinstance(error, APITimeoutError):
        return "MODEL_TIMEOUT"
    if isinstance(error, APIStatusError) and error.status_code >= 500:
        return "MODEL_5XX"
    if isinstance(error, BadOutput):
        return "BAD_OUTPUT"
    if isinstance(error, ToolResultUnknown):
        return "TOOL_UNKNOWN"
    if isinstance(error, ToolError):
        return "TOOL_ERROR"
    return "INTERNAL_ERROR"


def _execute_run_body(
    run_id: str,
    *,
    session_factory: sessionmaker[Session] = SessionLocal,
    client: ChatClient | None = None,
    worker_id: str | None = None,
    enable_renewer: bool = True,
    pause_after_tool_seconds: float | None = None,
) -> bool:
    owner = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
    fence = claim_run(
        session_factory,
        run_id,
        owner,
        lease_seconds=settings.lease_seconds,
    )
    if fence is None:
        return False
    run_started = time.perf_counter()

    with session_factory.begin() as session:
        prompt, created_at = session.execute(
            select(AgentRun.input_json["prompt"].astext, AgentRun.created_at).where(AgentRun.id == run_id)
        ).one()
        append_event(session, run_id, "run.started", {"attempt": fence.attempt}, fence=fence)
    get_harness_metrics().record_queue_lag((datetime.now(timezone.utc) - created_at).total_seconds())

    stop_renewal = threading.Event()

    def renewal_loop() -> None:
        while not stop_renewal.wait(10):
            if not renew_lease(
                session_factory,
                run_id,
                fence,
                lease_seconds=settings.lease_seconds,
            ):
                return

    renewal_thread = None
    if enable_renewer:
        renewal_thread = threading.Thread(target=renewal_loop, daemon=True)
        renewal_thread.start()

    try:
        result = run_agent(
            session_factory,
            fence,
            run_id,
            str(prompt),
            client or OllamaClient(),
            pause_after_tool_seconds=(
                settings.harness_test_pause_after_tool_seconds
                if pause_after_tool_seconds is None
                else pause_after_tool_seconds
            ),
        )
        finished = finish_run(
            session_factory,
            run_id,
            fence,
            status="completed",
            result=result,
        )
        if finished:
            get_harness_metrics().record_run_outcome(
                time.perf_counter() - run_started, "completed"
            )
        return finished
    except Exception as exc:
        error_code = classify_error(exc)
        if error_code == "INTERNAL_ERROR":
            capture_sentry_exception(exc, run_id)
        finished = finish_run(
            session_factory,
            run_id,
            fence,
            status="failed",
            error_code=error_code,
        )
        if finished:
            get_harness_metrics().record_run_outcome(
                time.perf_counter() - run_started, "failed", error_code
            )
        return finished
    finally:
        stop_renewal.set()
        if renewal_thread is not None:
            renewal_thread.join(timeout=2)


def _rq_trace_context() -> dict[str, str]:
    try:
        from rq import get_current_job

        job = get_current_job()
    except Exception:
        return {}
    if job is None or not isinstance(job.meta, dict):
        return {}
    carrier = job.meta.get("trace_context", {})
    return carrier if isinstance(carrier, dict) else {}


def execute_run(
    run_id: str,
    *,
    session_factory: sessionmaker[Session] = SessionLocal,
    client: ChatClient | None = None,
    worker_id: str | None = None,
    enable_renewer: bool = True,
    pause_after_tool_seconds: float | None = None,
) -> bool:
    bound_engine = session_factory.kw.get("bind") if hasattr(session_factory, "kw") else None
    runtime = initialize_observability("harness-worker", engine=bound_engine)
    try:
        with span_from_carrier(
            "harness.execute_run",
            _rq_trace_context(),
            attributes={"run.id": run_id},
        ):
            return _execute_run_body(
                run_id,
                session_factory=session_factory,
                client=client,
                worker_id=worker_id,
                enable_renewer=enable_renewer,
                pause_after_tool_seconds=pause_after_tool_seconds,
            )
    finally:
        if runtime.enabled:
            try:
                runtime.force_flush()
            except Exception:
                pass
            try:
                runtime.shutdown()
            except Exception:
                pass
