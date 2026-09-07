# Harness Lab M1 Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify one durable run lifecycle from FastAPI through PostgreSQL outbox, Redis/RQ, an Ollama-backed worker, SSE replay, and lease-based recovery.

**Architecture:** PostgreSQL is the only source of truth. The API commits runs, events, idempotency keys, and outbox jobs without touching Redis; a dispatcher bridges committed outbox rows into RQ; workers claim a database lease before executing; a sweeper requeues expired work. Every persistent Lab-owned byte uses a bind mount below `harness-lab/data/`; Ollama remains the already-running host process.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, psycopg 3, Alembic, PostgreSQL 16, Redis 7, RQ, OpenAI Python client, Uvicorn, Docker Compose, stdlib/pytest tests.

**Execution constraints:** Work only below `harness-lab/`. Do not initialize Sentry, Langfuse, or OpenTelemetry before M2. Do not increase Docker Desktop beyond the current roughly 7 GB allocation. Do not commit or push. Use TDD for every behavior task and update `docs/EXPERIMENTS.md` from saved evidence only.

---

## File map

- `pyproject.toml`, `uv.lock`: Lab-only runtime and test dependencies.
- `Dockerfile`, `compose.yaml`: one application image plus PostgreSQL/Redis and four application processes; bind-mounted Lab persistence only.
- `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_m1_schema.py`: initial schema and seed campaigns.
- `app/config.py`: one typed environment contract.
- `app/db.py`: engine/session factories and transaction helpers.
- `app/models.py`: ORM schema; no service logic.
- `app/events.py`: the single event-sequence allocator; locks the run row before `max(sequence)+1` and requires a current worker fence for worker-authored events.
- `app/runs.py`: create/get/idempotency transaction service.
- `app/outbox.py`: claim, dispatch result, and retry bookkeeping.
- `app/leases.py`: claim, renew, expire, requeue, and max-attempt transitions.
- `app/api/main.py`, `app/api/routes_runs.py`: HTTP and SSE boundary.
- `app/agent/tools.py`, `app/agent/llm.py`, `app/agent/loop.py`: tool schemas/execution, Ollama adapter, bounded agent loop.
- `app/jobs.py`, `app/worker.py`: importable RQ job and worker process entrypoint.
- `app/dispatcher.py`, `app/sweeper.py`: independent polling processes.
- `app/chaos/proxy.py`: importable M3 placeholder with no active fault behavior.
- `web/index.html`: no-build run creator and event viewer.
- `scripts/verify_m1.py`: automated Gate M1 checks and JSON evidence.
- `tests/`: unit, database-integration, API, and system tests grouped by responsibility.

### Task 1: Lab dependency and container foundation

**Files:**
- Create: `harness-lab/pyproject.toml`
- Create: `harness-lab/uv.lock`
- Create: `harness-lab/Dockerfile`
- Create: `harness-lab/compose.yaml`
- Modify: `harness-lab/config/.env.example`
- Modify: `harness-lab/.gitignore`
- Test: `harness-lab/tests/test_m1_structure.py`

- [x] Write a failing structure test asserting every Compose bind source resolves below the Lab, `env_file` is `./secrets/.env`, Ollama uses `host.docker.internal`, health checks exist, and no observability SDK is installed.
- [x] Run `harness-lab/.venv/bin/python -m unittest harness-lab/tests/test_m1_structure.py -v`; expect failure because M1 files do not exist.
- [x] Add the minimal dependency manifest and single application image; include `sse-starlette`; declare `postgres`, `redis`, `migrate`, `api`, `dispatcher`, `worker`, and `sweeper` services.
- [x] Use `./data/postgres:/var/lib/postgresql/data` and `./data/redis:/data`; never declare a named volume.
- [x] Set hard service limits: PostgreSQL 768 MiB, Redis 256 MiB, API 512 MiB, dispatcher 256 MiB, worker 1 GiB, sweeper 256 MiB, and one-shot migration 256 MiB (3.25 GiB maximum including migration; 3.0 GiB steady-state maximum). Set `restart: unless-stopped` for the worker and health-based dependencies.
- [x] Add database, Redis, lease, and worker variables to the template and user secret file without changing existing cloud keys.
- [x] Run `uv lock --project harness-lab` and `uv sync --project harness-lab --dev`; require all packages to install only into `harness-lab/.venv`.
- [x] Run `harness-lab/.venv/bin/python -m unittest harness-lab/tests/test_m1_structure.py -v` and `docker compose --env-file harness-lab/secrets/.env -f harness-lab/compose.yaml config`; expect PASS and a valid rendered config.

### Task 2: Configuration, ORM schema, and migration

**Files:**
- Create: `harness-lab/app/__init__.py`
- Create: `harness-lab/app/config.py`
- Create: `harness-lab/app/db.py`
- Create: `harness-lab/app/models.py`
- Create: `harness-lab/alembic.ini`
- Create: `harness-lab/alembic/env.py`
- Create: `harness-lab/alembic/versions/0001_m1_schema.py`
- Test: `harness-lab/tests/test_schema.py`

- [x] Write failing tests for all seven required tables, partial outbox index, uniqueness constraints, timestamp types, campaign seed rows, and separate `DATABASE_URL` (Compose hostname) / `TEST_DATABASE_URL` (host port) settings.
- [x] Run the schema test; expect missing-module failure.
- [x] Implement the exact M1 fields from `docs/harness-lab-plan.md`; add only required foreign keys/check constraints and UTC timestamps.
- [x] Run PostgreSQL and the migration service, then inspect `alembic current` and campaign count.
- [x] Run `TEST_DATABASE_URL=postgresql+psycopg://postgres:harness@127.0.0.1:5432/harness harness-lab/.venv/bin/python -m unittest harness-lab/tests/test_schema.py -v`; expect PASS with exactly three campaigns.

### Task 3: Atomic run creation and request idempotency

**Files:**
- Create: `harness-lab/app/events.py`
- Create: `harness-lab/app/runs.py`
- Test: `harness-lab/tests/test_runs_service.py`

- [x] Write failing integration tests proving one transaction creates exactly one `agent_runs`, `run.created`, and pending `outbox_jobs` row; rollback leaves none.
- [x] Add a concurrency test sending the same idempotency key three times and asserting one run ID and one database row.
- [x] Implement a ULID-like `run_` ID generator, row-lock-based event append, and a single transaction service that never imports Redis.
- [x] Run `TEST_DATABASE_URL=postgresql+psycopg://postgres:harness@127.0.0.1:5432/harness harness-lab/.venv/bin/python -m unittest harness-lab/tests/test_runs_service.py -v`; expect PASS.

### Task 4: HTTP API, polling, and SSE replay

**Files:**
- Create: `harness-lab/app/api/__init__.py`
- Create: `harness-lab/app/api/main.py`
- Create: `harness-lab/app/api/routes_runs.py`
- Create: `harness-lab/web/index.html`
- Test: `harness-lab/tests/test_api.py`
- Test: `harness-lab/tests/test_sse.py`

- [x] Write failing API tests for `POST /runs`, optional `Idempotency-Key`, `GET /runs/{id}`, 404s, and response schemas.
- [x] Write failing SSE tests for `Last-Event-ID`, ascending unique sequence IDs, heartbeat comments, and stream closure after terminal events.
- [x] Implement routes with `sse_starlette.sse.EventSourceResponse`, 500 ms polling, and 15 s heartbeats; mount the no-build viewer at `/`.
- [x] Run `TEST_DATABASE_URL=postgresql+psycopg://postgres:harness@127.0.0.1:5432/harness harness-lab/.venv/bin/python -m unittest harness-lab/tests/test_api.py harness-lab/tests/test_sse.py -v`; expect PASS with Redis stopped.

### Task 5: Transactional outbox dispatcher

**Files:**
- Create: `harness-lab/app/outbox.py`
- Create: `harness-lab/app/dispatcher.py`
- Test: `harness-lab/tests/test_outbox.py`

- [x] Write failing tests for `FOR UPDATE SKIP LOCKED`, batches of 100, one `run.enqueued` event, duplicate RQ job handling, and exponential retry capped at 60 s.
- [x] Implement one polling iteration as a testable function plus a one-second process loop.
- [x] Mark an outbox row dispatched only after Redis accepts or confirms deterministic RQ-safe `job_id={run_id}_outbox_{outbox_id}` (RQ 2.12 permits only letters, numbers, underscores, and dashes). This deduplicates retries of one delivery while allowing a sweeper-created outbox row to redeliver the run; run-level idempotency is enforced by the database fence. On failure retain pending status and persist attempts/next attempt/error.
- [x] Run `TEST_DATABASE_URL=postgresql+psycopg://postgres:harness@127.0.0.1:5432/harness harness-lab/.venv/bin/python -m unittest harness-lab/tests/test_outbox.py -v` with two concurrent dispatcher calls; expect every row dispatched at most once.

### Task 6: Tool contracts and side-effect idempotency

**Files:**
- Create: `harness-lab/app/agent/__init__.py`
- Create: `harness-lab/app/agent/tools.py`
- Test: `harness-lab/tests/test_tools.py`

- [x] Write failing tests for the three JSON schemas, exact campaign reads, deterministic reports, canonical argument hashing, and `abs(delta) <= current budget * 0.20`.
- [x] Write a replay/concurrency test proving duplicate `adjust_budget` calls create exactly one `budget_audit` row and change the budget once; pass an explicit immutable `WorkerFence(lease_owner, attempt)` into every worker tool call.
- [x] Implement read tools and the write tool through one `tool_calls` idempotency gate and one database transaction. Before a side effect, lock `agent_runs` and require `status=running`, matching owner/attempt, and an unexpired lease; a stale fence performs no write.
- [x] Treat existing `succeeded` as replay, and `executing`/`unknown` as `TOOL_UNKNOWN`.
- [x] Run `TEST_DATABASE_URL=postgresql+psycopg://postgres:harness@127.0.0.1:5432/harness harness-lab/.venv/bin/python -m unittest harness-lab/tests/test_tools.py -v`; expect PASS.

### Task 7: Ollama adapter and bounded agent loop

**Files:**
- Create: `harness-lab/app/agent/llm.py`
- Create: `harness-lab/app/agent/loop.py`
- Test: `harness-lab/tests/test_agent_loop.py`

- [x] Write failing fake-client tests for tool-call then final-answer flow, malformed arguments, unknown tools, model timeout/429 classification, and the six-round bound.
- [x] Implement the OpenAI client from settings, preserving `/no_think`, exact IDs, tool schema limit, and M0-compatible prompting.
- [x] Append public-only model/tool events through the current `WorkerFence`; never include credentials, full exception bodies, or private configuration.
- [x] Run `harness-lab/.venv/bin/python -m unittest harness-lab/tests/test_agent_loop.py -v`, then its opt-in live Ollama case; expect a completed answer for `camp_001`.

### Task 8: Lease-safe RQ worker

**Files:**
- Create: `harness-lab/app/leases.py`
- Create: `harness-lab/app/jobs.py`
- Create: `harness-lab/app/worker.py`
- Test: `harness-lab/tests/test_leases.py`
- Test: `harness-lab/tests/test_job_execution.py`

- [x] Write failing tests for atomic claim returning `WorkerFence(owner, attempt)`, refusal while a live lease exists, attempt increment, ten-second renewal, terminal lease cleanup, and exception-to-error-code mapping.
- [x] Add a stale-worker race: after lease expiry and a new claim, the old fence must fail renewal, event append, side-effect entry, and terminal transition; assert one terminal event and one budget mutation.
- [x] Implement the importable `execute_run(run_id)` job and a worker entrypoint with a unique worker ID.
- [x] Keep renewal lifecycle bounded to the job and make all final status/event writes conditional on matching owner + attempt + running status + unexpired lease.
- [x] Add a disabled-by-default `HARNESS_TEST_PAUSE_AFTER_TOOL_SECONDS` synchronization hook used only by Gate G5 after the first attempt's committed `adjust_budget` result; production default is zero and recovery attempts never repeat the artificial pause.
- [x] Run `TEST_DATABASE_URL=postgresql+psycopg://postgres:harness@127.0.0.1:5432/harness harness-lab/.venv/bin/python -m unittest harness-lab/tests/test_leases.py harness-lab/tests/test_job_execution.py -v`; with two claimants, expect one current fence and one terminal event.

### Task 9: Sweeper recovery

**Files:**
- Create: `harness-lab/app/sweeper.py`
- Test: `harness-lab/tests/test_sweeper.py`

- [x] Write failing tests for the five-second expiry grace, requeue through a fresh outbox row, preserved attempt count, one `run.requeued_by_sweeper` event, and `MAX_RETRY` at attempt three.
- [x] Implement one transactional sweep iteration plus a ten-second process loop.
- [x] Run `TEST_DATABASE_URL=postgresql+psycopg://postgres:harness@127.0.0.1:5432/harness harness-lab/.venv/bin/python -m unittest harness-lab/tests/test_sweeper.py -v` with competing sweepers; expect expired runs to transition once with no duplicate recovery outbox rows.

### Task 10: M3 placeholder contract

**Files:**
- Create: `harness-lab/app/chaos/__init__.py`
- Create: `harness-lab/app/chaos/proxy.py`
- Test: `harness-lab/tests/test_chaos_placeholder.py`

- [x] Write a failing import/route test requiring a health endpoint while rejecting fault-injection configuration in M1.
- [x] Add the smallest FastAPI placeholder; it must not be included in M1 Compose and must not proxy traffic yet.
- [x] Run `harness-lab/.venv/bin/python -m unittest harness-lab/tests/test_chaos_placeholder.py -v`; expect PASS.

### Task 11: Baseline bring-up and automated M1 gate

**Files:**
- Create: `harness-lab/scripts/verify_m1.py`
- Create: `harness-lab/tests/test_verify_m1.py`
- Modify: `harness-lab/docs/EXPERIMENTS.md`
- Evidence: `harness-lab/artifacts/m1/gate_m1.json`

- [x] Write failing verifier tests for the ordered lifecycle `created → enqueued → started → tool_call → tool_result → completed`, 20-run completion rate, POST p95, SSE resume continuity, triple request idempotency, worker-kill recovery, exact-once budget audit, and Redis-outage outbox recovery.
- [x] Record Docker allocation, bring up the M1 stack, and save `docker stats --no-stream`, inspect state, and restart counts. Pass only when aggregate running-container RSS is <=4 GiB, every container has `OOMKilled=false`, and non-test restart count is zero; stop and tune instead of increasing Docker memory.
- [x] Run the normal 20-run arm; require completion >=18/20, zero failures except at most two `BAD_OUTPUT`, the full ordered lifecycle for every completed run, and POST p95 <100 ms.
- [x] Run SSE and request-idempotency checks; require exact sequences and one run row.
- [x] For G5 create one budget-adjust run with `HARNESS_TEST_PAUSE_AFTER_TOOL_SECONDS` enabled, wait until its single committed `budget_audit` row is visible, then `docker kill` the worker and immediately run the declared idempotent Compose worker restart step. Require the replacement consumer, sweeper requeue/new fenced claim, completion within 60 s, one budget change, one audit row, and one terminal event.
- [x] Stop Redis, create runs, restart Redis, and require backlog dispatch within 30 s.
- [x] Save machine-readable results under `artifacts/m1/`, update `docs/EXPERIMENTS.md`, and run the entire Lab test suite.
- [x] Do not enter M2 unless every M1 gate passes and the user approves the next stage.

## Verification commands

```bash
harness-lab/.venv/bin/python -W error::ResourceWarning -m unittest discover -s harness-lab/tests -v
docker compose --env-file harness-lab/secrets/.env -f harness-lab/compose.yaml config
docker compose --env-file harness-lab/secrets/.env -f harness-lab/compose.yaml up -d --build
harness-lab/.venv/bin/python harness-lab/scripts/verify_m1.py
docker compose --env-file harness-lab/secrets/.env -f harness-lab/compose.yaml ps
docker stats --no-stream
```

Expected final state: all Lab tests pass; Gate M1 JSON reports G1-G6 PASS; all running containers are healthy or running without restart churn; no secret value appears outside `harness-lab/secrets/.env`; no Lab-owned persistent path exists outside `harness-lab/`.
