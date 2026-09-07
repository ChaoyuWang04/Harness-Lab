# M2 Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render final answers correctly and add an evidence-backed observability layer spanning Grafana LGTM, OpenTelemetry, Langfuse, and Sentry.

**Architecture:** Preserve PostgreSQL and the M1 state machine as the source of truth. Add one shared telemetry boundary, persist W3C context through Outbox and RQ metadata, provision dashboards as files, and keep cloud observability failure non-fatal.

**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL, Redis/RQ, OpenTelemetry Python, Grafana `otel-lgtm`, Langfuse Python SDK, Sentry Python SDK, pytest.

**Command convention:** Every relative path and every command below is evaluated with `/Users/samwong/Desktop/1Project/Syncopate_Async_AgenticRL/harness-lab` as the working directory.

---

### Task 1: Human-readable final answer

**Files:**
- Modify: `web/index.html`
- Test: `tests/test_m2_web.py`

- [ ] Write a static behavior test requiring JSON parsing, a dedicated answer region, readable answer assignment, error rendering, and wrapped raw events.
- [ ] Run `uv run pytest tests/test_m2_web.py -q` and confirm it fails for the missing result panel.
- [ ] Implement the minimal DOM/CSS/event-handler change; do not build a general chat UI or multi-run dashboard.
- [ ] Run `uv run pytest tests/test_m2_web.py tests/test_sse.py tests/test_api.py -q`; require PASS.

### Task 2: Dependencies, settings, and resource preflight

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `app/config.py`, `compose.yaml`, `config/.env.example`
- Test: `tests/test_m2_structure.py`, `tests/test_config.py`

- [ ] From working directory `harness-lab/`, write failing tests for `opentelemetry-sdk`, OTLP exporter, FastAPI/SQLAlchemy/requests/httpx/Redis instrumentors, Langfuse, compatible Sentry 2.x, opt-in settings, LGTM `mem_limit: 2g`, OTLP environment, and secret-free configuration. Run `uv run pytest tests/test_m2_structure.py tests/test_config.py -q`; expect assertion failures for missing dependencies/settings.
- [ ] Run `uv add opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc opentelemetry-instrumentation-fastapi opentelemetry-instrumentation-sqlalchemy opentelemetry-instrumentation-requests opentelemetry-instrumentation-httpx opentelemetry-instrumentation-redis langfuse 'sentry-sdk[fastapi]<3'`; record resolved versions from `uv.lock` and the Sentry pin reason.
- [ ] Add settings with telemetry enabled in Compose but safely disabled for isolated unit tests.
- [ ] Run `uv run pytest tests/test_m2_structure.py tests/test_config.py -q`; require PASS.
- [ ] On the actual Compose host, capture hostname, Docker memory, the eight required running services, RSS, restart, and OOM state in `artifacts/m2/resource_preflight.json`; require aggregate RSS `<4 GiB`, OOM 0, and unexpected restarts 0. Do not compare the migrated 5090 host to the historical Mac Docker allocation.

### Task 3: Shared telemetry and debug counts

**Files:**
- Create: `app/telemetry.py`, `app/api/routes_metrics.py`
- Modify: `app/api/main.py`
- Test: `tests/test_telemetry.py`, `tests/test_metrics_debug.py`

- [ ] Write failing tests for the exact instrument table in the design, non-fatal disabled exporters, the single-provider/processor topology, Sentry scrubbing/run context, and aggregate debug counts. Run `uv run pytest tests/test_telemetry.py tests/test_metrics_debug.py -q`; expect missing-module failures.
- [ ] Implement initialization once per non-forked process and once after fork per RQ workhorse. Programmatically activate FastAPI, SQLAlchemy, requests, httpx, and Redis instrumentors; attach one OTLP exporter and current Langfuse default-filter processor to the shared provider; keep Sentry error-only.
- [ ] Implement `GET /metrics/debug` with pending Outbox and per-status run counts.
- [ ] Run `uv run pytest tests/test_telemetry.py tests/test_metrics_debug.py -q`; require PASS before the full unit suite.

### Task 4: Durable distributed trace propagation

**Files:**
- Modify: `app/api/routes_runs.py`, `app/runs.py`, `app/outbox.py`, `app/jobs.py`
- Test: `tests/test_outbox.py`, `tests/test_job_execution.py`, `tests/test_m2_trace_context.py`

- [ ] Write failing tests proving actual trace ID and parent span ID continuity across API → persisted Outbox carrier → dispatcher span → RQ metadata → RQ workhorse span; carrier extraction remains optional for direct unit tests. Run `uv run pytest tests/test_m2_trace_context.py tests/test_outbox.py tests/test_job_execution.py -q`; expect missing-carrier assertions.
- [ ] Implement W3C inject/extract at the API, dispatcher, and worker boundaries. Langfuse inherits that active trace; attach `run_id` as an OTel trace attribute and Langfuse correlation/session field rather than creating a second deterministic trace ID.
- [ ] Add spans for dispatch, execute, model, and tool without weakening lease fencing or transactional boundaries.
- [ ] Run `uv run pytest tests/test_m2_trace_context.py tests/test_outbox.py tests/test_job_execution.py tests/test_leases.py tests/test_sweeper.py -q`; require PASS and matching trace/parent IDs.

### Task 5: Metrics at their owning transitions

**Files:**
- Modify: `app/jobs.py`, `app/agent/llm.py`, `app/agent/loop.py`, `app/agent/tools.py`, `app/outbox.py`, `app/sweeper.py`
- Test: `tests/test_m2_metrics.py`

- [ ] Write failing tests for claim-time queue lag, continuously sampled oldest queued age, committed run duration/outcome, model status, real tool latency excluding replay, Outbox pending gauge, and committed swept-run measurements. Run `uv run pytest tests/test_m2_metrics.py -q`; expect missing measurements.
- [ ] Change the tool boundary to return an explicit `{result, executed_now}` outcome. Set `executed_now=true` only after the first tool transaction commits; cached idempotent replay returns false. Record latency only for true.
- [ ] Add the remaining minimal measurements with bounded labels and monotonic timings.
- [ ] Run `uv run pytest tests/test_m2_metrics.py tests/test_tools.py tests/test_agent_loop.py tests/test_job_execution.py -q`; require PASS and prove telemetry failure cannot change run completion or tool idempotency.

### Task 6: Langfuse and Sentry integration

**Files:**
- Modify: `app/agent/llm.py`, `app/api/main.py`, `app/jobs.py`, `app/telemetry.py`
- Test: `tests/test_langfuse_integration.py`, `tests/test_sentry_integration.py`

- [ ] Write failing adapter tests with local fakes; do not send cloud events during unit tests. Require prompt, completion/tool-call output, latency, and prompt/completion/total token counts for every expected model round. Run `uv run pytest tests/test_langfuse_integration.py tests/test_sentry_integration.py -q`; require failures caused by the missing adapters/flush behavior.
- [ ] Add the locked current Langfuse API (`get_client`, `start_as_current_observation(as_type="generation", ...)`) at the model boundary, inheriting the shared distributed trace and attaching `run_id` as correlation/session metadata.
- [ ] Initialize Sentry error reporting in API and inside the RQ workhorse. Add a disabled-by-default CLI verifier carrying `run_id`, returning a known event ID, and proving cloud receipt without exposing a public exception route.
- [ ] In every RQ workhorse `finally`, perform bounded non-fatal OTel/Langfuse force-flush and shutdown; flush Sentry there whenever an event was captured. Use a longer blocking wait only in the Gate verifier.
- [ ] Run `uv run pytest tests/test_langfuse_integration.py tests/test_sentry_integration.py -q`; require PASS, including workhorse-finally flush assertions.

### Task 7: Reproducible Grafana dashboard and alerts

**Files:**
- Create: `config/grafana/provisioning/datasources/datasources.yaml`, `config/grafana/provisioning/dashboards/dashboards.yaml`, `config/grafana/provisioning/alerting/rules.yaml`, `config/grafana/dashboards/harness-slo.json`
- Modify: `compose.yaml`
- Test: `tests/test_grafana_provisioning.py`

- [ ] Write failing structural tests for four panels, required threshold lines, stable datasource UIDs, and two alert rules. Assert the queue alert expression is exactly `max(agent_oldest_queued_age_seconds) > 10` with a two-minute pending period.
- [ ] Provision LGTM and mount only Lab-local configuration.
- [ ] Start LGTM with `mem_limit: 2g`, confirm Grafana, collector, Tempo, metrics backend, and Loki readiness, then require aggregate RSS `<4 GiB`, OOM 0, and unexpected restarts 0 before load.
- [ ] Run `uv run pytest tests/test_grafana_provisioning.py -q`; require PASS before live dashboard checks.

### Task 8: Gate M2 verification and documentation

**Files:**
- Create: `scripts/verify_m2.py`, `artifacts/m2/`
- Modify: `docs/EXPERIMENTS.md`, `README.md`
- Test: `tests/test_verify_m2.py`

- [ ] Write verifier tests for redaction, exact run count, trace span coverage, numeric metric extraction, Langfuse generation coverage, Sentry event identity, alert Firing state, and resource stop conditions. Run `uv run pytest tests/test_verify_m2.py -q`; require failures caused by the missing verifier.
- [ ] Implement `scripts/verify_m2.py`. It must create exactly 30 runs, then query Tempo/Grafana metrics APIs to assert the required API/dispatcher/workhorse/model/tool span coverage, all four dashboard query results are non-empty, and queue-lag P95/run P95/failed-rate are concrete numeric values. It must query Langfuse observations for per-round prompt/completion/token completeness, consume the separately verified Sentry event ID/run ID and Grafana alert result, re-read Docker allocation/RSS/OOM/restarts, and write one redacted `artifacts/m2/gate_m2.json` only after all checks have run.
- [ ] Run `uv run pytest tests/test_verify_m2.py -q`; require PASS.
- [ ] From `harness-lab/`, run `TEST_DATABASE_URL=postgresql+psycopg://postgres:harness@127.0.0.1:5432/harness_test TEST_REDIS_URL=redis://127.0.0.1:6379/0 uv run pytest -q`; require all non-opt-in tests pass.
- [ ] Rebuild/restart Lab services and run `uv run python scripts/verify_m1.py`; require all six M1 gates remain PASS.
- [ ] Do not create a separate load cohort here: the final verifier is the sole owner of one exactly-30-run cohort, records all 30 run IDs plus exact start/end timestamps, and forbids other run producers during that window. Tempo/Langfuse filter by the recorded run attributes/session; Grafana metric queries filter by the cohort time boundaries because `run_id` is intentionally not a metric label.
- [ ] Run the disabled-by-default Sentry verifier and save only its event ID/run ID. Open Sentry and save a user-visible issue screenshot proving that exact event ID and `run_id`; an ingestion response alone does not pass G4. Recursively scan artifacts/config for the DSN, authorization/cookie values, and Langfuse keys.
- [ ] Negative alert control: with workers running and no queued runs, observe `max(agent_oldest_queued_age_seconds) <= 10` and Normal for two minutes. Positive control: stop all workers, create one run, require `max(agent_oldest_queued_age_seconds) > 10` continuously for two minutes and Grafana Firing, then immediately restore the worker and require recovery.
- [ ] Inspect one real RQ-workhorse Tempo trace and assert trace ID/parent continuity plus API, dispatcher, worker, every model call, and every tool span. Query one Langfuse run and fail if any expected round lacks prompt, completion, or prompt/completion/total token counts.
- [ ] Run `uv run python scripts/verify_m2.py` on `home-5090`; require its sole cohort to contain exactly 30 terminal runs, Tempo/Langfuse queries to filter by those run IDs, Grafana queries to use the recorded exclusive cohort time window, four non-empty panel results, three recorded numeric baselines, all five Gate booleans true, all eight required services present, final aggregate RSS `<4 GiB`, OOM 0, unexpected restarts 0, and a redacted `artifacts/m2/gate_m2.json`.
- [ ] Update `docs/EXPERIMENTS.md` with predictions, actuals, resource readings, three numeric baselines, and at most five lines reserved for the user's later subjective comparison.
- [ ] Mark M2 passed in `README.md` only if all five gates pass; otherwise record the exact open gate without weakening it.

No commit or push is included: the repository instruction requires explicit user authorization for either action.
