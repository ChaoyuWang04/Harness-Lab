from __future__ import annotations

import os
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.metrics import Observation
from opentelemetry.propagate import extract, inject
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import settings


RUN_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled"})
ERROR_CODES = frozenset(
    {"MODEL_429", "MODEL_TIMEOUT", "MODEL_5XX", "BAD_OUTPUT", "TOOL_UNKNOWN", "TOOL_ERROR", "INTERNAL_ERROR", "MAX_RETRY"}
)
HTTP_STATUSES = frozenset({"200", "429", "timeout", "error"})
TOOL_NAMES = frozenset({"get_campaign", "get_report", "adjust_budget"})
SWEEP_OUTCOMES = frozenset({"requeued", "max_retry"})
LANGFUSE_CLOUD_HOSTS = frozenset(
    {
        "cloud.langfuse.com",
        "us.cloud.langfuse.com",
        "jp.cloud.langfuse.com",
        "hipaa.cloud.langfuse.com",
    }
)


def normalize_langfuse_base_url(value: str) -> str:
    candidate = value.strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {'"', "'"}:
        candidate = candidate[1:-1].strip()
    candidate = candidate.rstrip("/")
    if candidate in LANGFUSE_CLOUD_HOSTS:
        candidate = f"https://{candidate}"
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("LANGFUSE_BASE_URL must be a valid HTTP(S) URL")
    return candidate


def _bounded(value: str, allowed: frozenset[str], label: str) -> str:
    if value not in allowed:
        raise ValueError(f"unsupported {label}: {value}")
    return value


def validate_run_status(value: str) -> str:
    return _bounded(value, RUN_STATUSES, "run status")


def validate_error_code(value: str) -> str:
    return _bounded(value, ERROR_CODES, "error code")


def validate_http_status(value: str) -> str:
    return _bounded(value, HTTP_STATUSES, "http status")


def scrub_sentry_event(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    request = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                key: value
                for key, value in headers.items()
                if key.lower() not in {"authorization", "cookie", "set-cookie", "x-api-key"}
            }
        request.pop("data", None)
    return event


@dataclass
class ObservabilityRuntime:
    enabled: bool
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    langfuse: Any | None = None

    def force_flush(self) -> None:
        if self.tracer_provider is not None:
            self.tracer_provider.force_flush(timeout_millis=settings.otel_export_timeout_ms)
        if self.meter_provider is not None:
            self.meter_provider.force_flush(timeout_millis=settings.otel_export_timeout_ms)
        if self.langfuse is not None:
            self.langfuse.flush()

    def shutdown(self) -> None:
        if os.getpid() in _sentry_dirty_pids:
            import sentry_sdk

            sentry_sdk.flush(timeout=settings.otel_export_timeout_ms / 1000)
            _sentry_dirty_pids.discard(os.getpid())
        if self.langfuse is not None:
            self.langfuse.shutdown()
        if self.meter_provider is not None:
            self.meter_provider.shutdown(timeout_millis=settings.otel_export_timeout_ms)
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()


_runtimes: dict[tuple[int, str], ObservabilityRuntime] = {}
_metric_recorders: dict[int, "HarnessMetrics"] = {}
_observable_instruments: list[Any] = []
_sentry_dirty_pids: set[int] = set()


class HarnessMetrics:
    def __init__(self, meter: Any) -> None:
        self.queue_lag = meter.create_histogram(
            "agent_queue_lag_seconds", unit="s", description="Time from run creation to worker claim"
        )
        self.run_duration = meter.create_histogram(
            "agent_run_duration_seconds", unit="s", description="Run wall duration"
        )
        self.run_total = meter.create_counter("agent_run_total", unit="{run}")
        self.run_failed = meter.create_counter("agent_run_failed_total", unit="{run}")
        # The metric name already carries the millisecond unit. A dimensionless
        # OTel unit prevents the Prometheus exporter from appending a second
        # `_milliseconds` suffix and keeps the public metric name stable.
        self.tool_latency = meter.create_histogram("agent_tool_latency_ms", unit="1")
        self.model_calls = meter.create_counter("agent_model_call_total", unit="{call}")
        self.swept_runs = meter.create_counter("stuck_runs_swept_total", unit="{run}")

    def record_queue_lag(self, seconds: float) -> None:
        self.queue_lag.record(max(0.0, seconds))

    def record_run_outcome(self, seconds: float, status: str, error_code: str | None = None) -> None:
        status = validate_run_status(status)
        self.run_duration.record(max(0.0, seconds), {"status": status})
        self.run_total.add(1, {"status": status})
        if status == "failed":
            self.run_failed.add(1, {"error_code": validate_error_code(error_code or "INTERNAL_ERROR")})

    def record_model_call(self, http_status: str) -> None:
        self.model_calls.add(1, {"http_status": validate_http_status(http_status)})

    def record_tool_latency(self, tool_name: str, milliseconds: float, *, executed_now: bool) -> None:
        if not executed_now:
            return
        tool_name = _bounded(tool_name, TOOL_NAMES, "tool name")
        self.tool_latency.record(max(0.0, milliseconds), {"tool_name": tool_name})

    def record_sweep(self, outcome: str) -> None:
        outcome = _bounded(outcome, SWEEP_OUTCOMES, "sweep outcome")
        self.swept_runs.add(1, {"outcome": outcome})


def get_harness_metrics() -> HarnessMetrics:
    process_id = os.getpid()
    if process_id not in _metric_recorders:
        _metric_recorders[process_id] = HarnessMetrics(metrics.get_meter("harness-lab"))
    return _metric_recorders[process_id]


def get_langfuse_client() -> Any | None:
    for (process_id, _service_name), runtime in _runtimes.items():
        if process_id == os.getpid() and runtime.langfuse is not None:
            from langfuse import get_client

            return get_client()
    return None


@contextmanager
def model_observation(
    run_id: str,
    step: int,
    messages: list[dict[str, Any]],
    model: str,
    *,
    client: Any | None = None,
) -> Iterator[Any]:
    langfuse = client or get_langfuse_client()
    if langfuse is None:
        tracer = trace.get_tracer("harness-lab.agent")
        with tracer.start_as_current_span(
            "harness.model_call",
            attributes={"run.id": run_id, "agent.step": step, "gen_ai.request.model": model},
        ) as span:
            yield span
        return

    from langfuse import propagate_attributes

    with propagate_attributes(
        session_id=run_id,
        metadata={"run_id": run_id},
        trace_name="harness-run",
    ):
        with langfuse.start_as_current_observation(
            as_type="generation",
            name="harness.model_call",
            input=messages,
            model=model,
            metadata={"run_id": run_id, "step": step},
        ) as observation:
            yield observation


def update_model_observation(
    observation: Any,
    *,
    output: dict[str, Any],
    usage: dict[str, int] | None,
) -> None:
    update = getattr(observation, "update", None)
    if callable(update):
        kwargs: dict[str, Any] = {"output": output}
        if usage is not None:
            kwargs["usage_details"] = usage
        update(**kwargs)
        return
    set_attribute = getattr(observation, "set_attribute", None)
    if callable(set_attribute) and usage is not None:
        for name, value in usage.items():
            set_attribute(f"gen_ai.usage.{name}_tokens", value)


def capture_sentry_exception(error: BaseException, run_id: str) -> str | None:
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("run_id", run_id)
        event_id = sentry_sdk.capture_exception(error)
    if event_id:
        _sentry_dirty_pids.add(os.getpid())
        return str(event_id)
    return None


def read_database_gauges(session_factory: Any) -> dict[str, float | int]:
    from sqlalchemy import func, select

    from app.models import AgentRun, OutboxJob

    with session_factory() as session:
        pending = session.scalar(
            select(func.count()).select_from(OutboxJob).where(OutboxJob.status == "pending")
        )
        oldest_age = session.scalar(
            select(func.extract("epoch", func.now() - func.min(AgentRun.created_at))).where(
                AgentRun.status == "queued"
            )
        )
    return {
        "outbox_pending_jobs": int(pending or 0),
        "agent_oldest_queued_age_seconds": float(oldest_age or 0),
    }


def register_database_gauges(session_factory: Any) -> None:
    meter = metrics.get_meter("harness-lab")

    def pending_callback(_options: Any) -> list[Observation]:
        return [Observation(read_database_gauges(session_factory)["outbox_pending_jobs"])]

    def oldest_callback(_options: Any) -> list[Observation]:
        return [Observation(read_database_gauges(session_factory)["agent_oldest_queued_age_seconds"])]

    _observable_instruments.extend(
        [
            meter.create_observable_gauge(
                "outbox_pending_jobs", callbacks=[pending_callback], unit="{job}"
            ),
            meter.create_observable_gauge(
                "agent_oldest_queued_age_seconds", callbacks=[oldest_callback], unit="s"
            ),
        ]
    )


def inject_trace_context() -> dict[str, str]:
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier


@contextmanager
def span_from_carrier(
    name: str,
    carrier: dict[str, str] | None,
    *,
    tracer: Any | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    active_tracer = tracer or trace.get_tracer("harness-lab")
    with active_tracer.start_as_current_span(
        name,
        context=extract(carrier or {}),
        attributes=attributes,
    ) as span:
        yield span


def _instrument_clients(engine: Any | None) -> None:
    RequestsInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=engine)


def _init_sentry() -> None:
    dsn = os.getenv("SENTRY_DSN", "")
    if not dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        traces_sample_rate=0,
        send_default_pii=False,
        before_send=scrub_sentry_event,
    )


def initialize_observability(
    service_name: str,
    *,
    enabled: bool | None = None,
    engine: Any | None = None,
) -> ObservabilityRuntime:
    active = settings.harness_observability_enabled if enabled is None else enabled
    if not active:
        return ObservabilityRuntime(enabled=False)

    key = (os.getpid(), service_name)
    if key in _runtimes:
        return _runtimes[key]

    tracer_provider = None
    meter_provider = None
    langfuse = None
    try:
        # Resource.create supplies a unique service.instance.id. That identity
        # must remain per-workhorse: the M2 cohort selects only instances born
        # after its start and therefore measures every short-lived RQ child.
        resource = Resource.create({"service.name": service_name, "deployment.environment": "harness-lab"})
        endpoint = settings.otel_exporter_otlp_endpoint
        tracer_provider = TracerProvider(resource=resource)
        metric_readers = []
        if settings.harness_otel_export_enabled:
            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=endpoint, insecure=endpoint.startswith("http://")),
                    export_timeout_millis=settings.otel_export_timeout_ms,
                )
            )
            metric_readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=endpoint, insecure=endpoint.startswith("http://")),
                    export_interval_millis=settings.otel_metric_export_interval_ms,
                    export_timeout_millis=settings.otel_export_timeout_ms,
                )
            )
        meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)

        langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
        langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")
        if langfuse_public_key and langfuse_secret_key:
            from langfuse import Langfuse

            langfuse = Langfuse(
                public_key=langfuse_public_key,
                secret_key=langfuse_secret_key,
                base_url=normalize_langfuse_base_url(
                    os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
                ),
                tracer_provider=tracer_provider,
            )

        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        _init_sentry()
        _instrument_clients(engine)
        runtime = ObservabilityRuntime(True, tracer_provider, meter_provider, langfuse)
        _runtimes[key] = runtime
        return runtime
    except Exception:
        for component in (langfuse, meter_provider, tracer_provider):
            if component is None:
                continue
            try:
                component.shutdown()
            except Exception:
                pass
        return ObservabilityRuntime(enabled=False)


def instrument_fastapi(application: Any) -> None:
    if not settings.harness_observability_enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(application)
