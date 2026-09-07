# M2 Observability Design

## Goal and boundary

M2 adds visibility to the already-passing M1 execution path without changing its reliability semantics. All code, Grafana provisioning, tests, and evidence remain below `harness-lab/`; Docker Desktop stays at the current roughly 7 GB allocation. Sentry and Langfuse credentials remain in `secrets/.env` and never appear in committed configuration, logs, dashboards, or artifacts.

The small UI correction is part of this round: the page must parse SSE JSON and render `run.completed.answer` as readable Chinese in a separate result panel while preserving a wrapped raw-event view for debugging.

## Chosen architecture

Use the existing single-container `grafana/otel-lgtm` development stack and lock resolved SDK versions in `uv.lock`. A focused `app/telemetry.py` module owns one global OpenTelemetry `TracerProvider`/`MeterProvider`, OTLP processors, instruments, explicit instrumentation, Langfuse attachment, and Sentry initialization; business modules call narrow helpers and do not configure exporters themselves.

The provider topology is deliberately singular. Each service process creates the global provider, attaches one OTLP batch processor for LGTM, then initializes the current Langfuse client on that provider with its default LLM-only export filter. FastAPI, SQLAlchemy, requests, httpx, and Redis instrumentors attach to this provider. Sentry is pinned to the latest compatible Python 2.x release and used for error reporting only (`traces_sample_rate=0`, `send_default_pii=False`), so it does not own or replace the OTel provider.

There is one distributed W3C trace identity: Langfuse generations inherit the active API→dispatcher→workhorse OTel trace. `run_id` is stored as a trace attribute and Langfuse session/correlation field; it is not converted into a second trace ID. This intentionally updates the plan's older shorthand “trace_id 用 run_id”: current Langfuse follows W3C 32-hex trace IDs, so correlation by run attribute preserves one real tree instead of promising two incompatible identities. Langfuse uses `get_client()` and `start_as_current_observation(as_type="generation", ...)`; every generation records its messages, completion/tool calls, model, latency, and `response.usage` prompt/completion/total token counts. Missing token counts fail G3 rather than being reported as complete.

RQ uses a fetch-fork-execute workhorse. The long-lived parent does not initialize exporters. `execute_run` initializes telemetry inside the workhorse after fork and performs a bounded, non-fatal OTel/Langfuse force-flush and shutdown in every `finally`; if that workhorse captured a Sentry event, it also performs a bounded Sentry flush. The Gate verifier may wait longer to confirm cloud receipt, but ordinary jobs must not rely on a verification-only flush. A real RQ child export is required by Gate evidence. API, dispatcher, and sweeper initialize once in their own non-forked process entry points.

Distributed trace propagation is durable. The API injects the current W3C carrier into the existing outbox payload in the same PostgreSQL transaction as run creation. Dispatcher extracts it, creates its span, and injects a child carrier into RQ job metadata. Worker extracts job metadata before starting the execute span. This preserves one API -> dispatcher -> worker -> model/tool tree across both asynchronous boundaries and across an Outbox retry.

Custom metrics use the names already registered in the lab plan. Counters and histograms are recorded at the transition that owns the fact; pending/running/status counts are queried from PostgreSQL by the internal `/metrics/debug` endpoint. A dispatcher-owned observable gauge continuously queries the age of the oldest queued run, including while no worker is alive, because a claim-only histogram cannot drive the required stopped-worker alert.

| Instrument | Type/unit | Recording point | Bounded labels |
|---|---|---|---|
| `agent_queue_lag_seconds` | histogram, seconds | once after a successful worker claim | none |
| `agent_oldest_queued_age_seconds` | observable gauge, seconds | dispatcher samples PostgreSQL every export interval; zero with no queued runs | none |
| `agent_run_duration_seconds` | histogram, seconds | only after committed terminal transition | `status` |
| `agent_run_total` | counter, runs | only after committed terminal transition | `status` |
| `agent_run_failed_total` | counter, runs | only after committed failed transition | `error_code` |
| `agent_tool_latency_ms` | histogram, milliseconds | after a real tool execution; cached idempotent replay is excluded | `tool_name` |
| `agent_model_call_total` | counter, calls | after each model attempt | `http_status` in `200`, `429`, `timeout`, `error` |
| `outbox_pending_jobs` | observable gauge, jobs | dispatcher samples PostgreSQL every export interval | none |
| `stuck_runs_swept_total` | counter, runs | only after the sweep transaction commits | `outcome` in `requeued`, `max_retry` |

Grafana provisioning is file-based under `config/grafana/`, so dashboard and alert definitions are reproducible Lab assets rather than click-only state. The 30-run verifier records an exclusive cohort start/end window and prevents other run producers during it; metrics are queried by that window, while Tempo and Langfuse use `run_id` attributes/session correlation. This preserves bounded metric cardinality. Because the small model may validly finish a run without selecting a tool, the verifier searches the bounded cohort for one run that has both the complete required Tempo span set and complete Langfuse generations; it does not assume the first run is representative. The queue-lag alert evaluates `max(agent_oldest_queued_age_seconds) > 10` for two minutes. Its negative control is two minutes with workers running and no backlog; its positive control stops all workers, creates a run, waits for Firing, and immediately restores the worker.

RQ workhorses are short-lived and export exactly one cumulative metric sample before exit. Dashboard queries therefore aggregate the collector-retained cumulative histogram buckets directly; `rate()` is invalid for these one-sample `service_instance_id` series and produces `NaN`. The displayed run-duration and queue-lag P95 values are explicitly scoped to the current telemetry-stack lifetime. Dashboard and alert failed-rate expressions use the same lifetime scope plus a zero fallback, so a healthy no-failure interval renders `0%` rather than `No data`. Model error counts and tool latency use separate axes because they have different units. The exclusive 30-run Gate still computes its cohort values from the before/after set of workhorse instance IDs rather than treating the lifetime Dashboard values as cohort evidence.

## Error and privacy behavior

- Telemetry export failure must not fail a run. Local execution and PostgreSQL remain authoritative.
- Sentry receives unexpected API/worker exceptions with `run_id`; known model/tool errors remain classified by the existing state machine. A disabled-by-default CLI verifier creates one known event ID and force-flushes it. Gate G4 requires a saved user-visible Sentry issue screenshot in `artifacts/m2/` showing that exact event ID and `run_id`; the verifier checks that both identifiers match the sent probe and that the screenshot exists inside the Lab. Ingestion HTTP 2xx alone is not acceptance.
- Langfuse records only the prompts and completions already sent to the local model. Secret values are never attached.
- Metric labels are bounded (`status`, `error_code`, `tool_name`, `http_status`); `run_id` is never a metric label.
- `/metrics/debug` is an internal diagnostic endpoint and exposes only aggregate counts.
- Sentry `before_send` removes authorization/cookie headers and request bodies. Gate artifacts are recursively scanned for the DSN and Langfuse keys before acceptance.

## Resource stop line

The runtime now lives on `home-5090`, so the old Mac Docker allocation `8,318,976,000` bytes is historical M0 evidence rather than a valid equality check. Run the verifier on the actual Compose host, record its hostname and Docker memory, and require all eight M2 services (`api`, `dispatcher`, `lgtm`, `ollama`, `postgres`, `redis`, `sweeper`, `worker`) to be running there. LGTM keeps its hard Compose limit of `2g`; no extra self-hosted Sentry or Langfuse components are enabled. Require Grafana, Tempo, Prometheus, Loki, and the collector to become ready and aggregate Lab RSS to remain below 4 GiB before starting the 30-run load. Recheck after the load. Stop immediately if aggregate Lab RSS reaches 4 GiB, any container is OOM-killed, an unexpected restart occurs, or the host becomes noticeably impaired. Mac Docker allocation is no longer part of this Gate because no Lab container runs on the Mac.

## Acceptance

The UI test proves Unicode answer rendering and raw-event retention. M2 then uses the five gates in `docs/harness-lab-plan.md`: one unbroken Tempo trace; four populated dashboard panels and three numeric baselines after 30 runs; complete Langfuse generations; a Sentry issue with `run_id`; and a real Grafana queue-lag alert in Firing state. Results come only from saved `artifacts/m2/` evidence and are summarized in `docs/EXPERIMENTS.md`.
