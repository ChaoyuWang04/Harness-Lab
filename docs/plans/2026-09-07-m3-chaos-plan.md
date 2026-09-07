# M3 Chaos and Load Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement and execute six deterministic M3 failure experiments on `home-5090`, preserve the normal Harness data, and produce a reproducible Gate M3 artifact.

**Architecture:** A profile-gated Chaos Proxy and one-shot dispatcher hook provide controlled faults. A runtime-host wrapper switches only application services to isolated PostgreSQL/Redis namespaces, runs one evidence-producing verifier, and restores the normal stack in a trap.

**Tech Stack:** FastAPI, httpx, OpenAI Python SDK, PostgreSQL/SQLAlchemy, Redis/RQ, Docker Compose, OpenTelemetry/Grafana, pytest.

---

### Task 1: Freeze the M3 runtime boundary

**Files:** `compose.yaml`, `app/config.py`, `tests/test_m3_structure.py`, `.env.example`, `docs/REMOTE-OPERATIONS.md`

- [ ] Write failing structure/config tests for profile-gated `chaos-proxy`, overridable database/Redis URLs, deterministic chaos settings, and a separate `harness_m3` wrapper.
- [ ] Run the focused tests and confirm they fail for missing M3 wiring.
- [ ] Add the minimum settings and Compose wiring; keep normal defaults unchanged.
- [ ] Run focused and full tests.
- [ ] Commit only Task 1 paths.

### Task 2: Implement deterministic provider faults and bounded retries

**Files:** `app/chaos/proxy.py`, `app/agent/loop.py`, `app/agent/llm.py`, `app/jobs.py`, `app/telemetry.py`, `tests/test_chaos_proxy.py`, `tests/test_agent_retry.py`

- [ ] Replace the M2 placeholder tests with failing forwarding, seeded 429/timeout/5xx, and health-state tests.
- [ ] Add failing retry tests for three retries, exponential delays, `step.model_retry`, per-attempt metrics, terminal classification, and Langfuse attempt boundaries.
- [ ] Run the focused tests and verify the expected failures.
- [ ] Implement the smallest proxy and retry path satisfying the tests.
- [ ] Run focused and full tests, then commit Task 2 paths.

### Task 3: Implement one-shot dispatcher crash injection

**Files:** `app/chaos/hooks.py`, `app/outbox.py`, `app/dispatcher.py`, `compose.yaml`, `tests/test_dispatcher_chaos.py`, `tests/test_outbox.py`

- [ ] Write failing tests proving the hook fires after enqueue, its marker makes it one-shot, and the Outbox transaction remains retryable.
- [ ] Run the tests and confirm the failure is caused by the missing hook.
- [ ] Implement marker-guarded exit injection without changing delivery identity or normal dispatch behavior.
- [ ] Run focused and full tests, then commit Task 3 paths.

### Task 4: Build the isolated runtime-host verifier

**Files:** `scripts/verify_m3.py`, `scripts/run_verify_m3_home5090.sh`, `tests/test_verify_m3.py`, `tests/test_home5090_deployment.py`, `docs/REMOTE-OPERATIONS.md`

- [ ] Write failing tests for exact experiment sizes, timestamp-derived metrics, restored services, duplicate-audit rejection, complete comparison table, secret redaction, and wrapper trap behavior.
- [ ] Run the tests and confirm expected missing-verifier failures.
- [ ] Implement orchestration helpers and the wrapper; no experiment may delete the normal database or broad-match containers.
- [ ] Run focused and full tests, then commit Task 4 paths.

### Task 5: Execute EXP-1 through EXP-3

**Files:** runtime artifacts under `artifacts/m3/`, `docs/EXPERIMENTS.md`

- [ ] Verify the normal queue is empty, all services are healthy, and resource RSS is below 4 GiB.
- [ ] Execute worker crash ten times and save recovery/audit evidence.
- [ ] Execute the one-shot dispatcher crash and save duplicate-delivery/run-fencing evidence.
- [ ] Execute the 60-second Redis outage and save POST/drain evidence.
- [ ] Stop on any failed registered threshold; diagnose before proceeding.

### Task 6: Execute EXP-4 through EXP-6 and Gate M3

**Files:** runtime artifacts under `artifacts/m3/`, `docs/EXPERIMENTS.md`, `README.md`, `AGENTS.md`

- [ ] Execute normal, 429, and timeout provider arms and observe alert recovery.
- [ ] Execute 500x50 load with one and four workers and compute the registered comparison.
- [ ] Execute 20-client, five-reconnect SSE storm and compare every sequence to PostgreSQL.
- [ ] Require all six experiment booleans, zero duplicate audit keys, complete 3x4 table, resource stop lines, and restored normal services.
- [ ] Run the full local test suite and remote health check.
- [ ] Record actual results, mark M3 complete only if every Gate is true, and commit/push named paths.

### Task 7: Post-Gate one-click demo controls

**Files:** to be fixed in a separate post-Gate design after Task 6

- [ ] Present a narrow design that keeps Docker access out of the public API and provides allowlisted, auto-recovering demonstrations.
- [ ] Obtain user approval for the exact buttons and safety model.
- [ ] Implement with TDD, verify on `home-5090`, and document how each button maps to saved M3 evidence.
