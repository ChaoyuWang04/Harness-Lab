# M3 Chaos and Load Design

## Goal and boundary

M3 proves the M1 reliability mechanisms under real process, queue, provider, load, and reconnect failures. The six experiments and thresholds remain those registered in `docs/harness-lab-plan.md`; this design only aligns their execution with the current `home-5090` runtime. The normal `harness` database is read-only during M3. Destructive runs use a persistent but separate `harness_m3` PostgreSQL database and Redis DB 1, with prompts tagged by experiment and all machine evidence below `artifacts/m3/`.

The ordinary stack is restored in an unconditional shell trap: one worker, Redis running, dispatcher/sweeper running, direct Ollama routing, and the normal database/Redis URL. A failed experiment retains its M3 database and evidence for diagnosis instead of deleting or silently rerunning it.

## Components

- `app/chaos/proxy.py` becomes a small OpenAI-compatible reverse proxy. It forwards to Lab Ollama and supports deterministic seeded 429, timeout, 5xx, and latency injection. `/health` reports only non-secret active rates and counters.
- `app/agent/loop.py` owns bounded model retry semantics. Each model attempt remains a separate Langfuse generation and metric sample. Retriable 429, timeout, and 5xx failures append `step.model_retry` with bounded error code, attempt, and backoff; at most three retries are made.
- `app/chaos/hooks.py` owns the dispatcher one-shot crash marker. The dispatcher exits after RQ publish but before its PostgreSQL transaction commits exactly once, then its normal restart retries the still-pending Outbox row. The real delivery ID remains `{run_id}_outbox_{outbox_id}`; duplicate delivery is rejected by RQ, while run ownership is fenced in PostgreSQL.
- `scripts/verify_m3.py` is the only experiment orchestrator. It records preconditions before each experiment, owns exact run IDs, performs failure injection and restoration, derives timings from PostgreSQL event timestamps, and writes one redacted gate plus per-experiment JSON.
- `scripts/run_verify_m3_home5090.sh` prepares `harness_m3`, starts the M3 profile, runs the verifier in the existing app image on the Compose network with narrowly required Docker access, and always restores the normal runtime.

## Experiment protocol

1. **Worker crash:** one worker, a bounded post-tool pause, and ten distinct `adjust_budget(+1)` runs. Kill only the resolved worker container after the committed tool result. Require at least 9/10 completion, every recovery start within 45 seconds, and one audit row per tool key.
2. **Dispatcher crash:** empty queue/outbox, enable the one-shot marker, create one run, observe dispatcher exit/restart and a still-pending Outbox row, then require exactly one started event and one terminal event.
3. **Redis outage:** stop only the Lab Redis container for 60 seconds while creating exactly one short run per second. Require 60/60 POST success and no dispatch while Redis is down; restore Redis, apply the bounded four-worker recovery policy, and require all 60 terminal plus pending Outbox zero within 120 seconds. The normal runtime still restores to one worker after the Gate.
4. **Provider degradation:** run a normal 30-run baseline, a deterministic 50% 429 arm, and a deterministic 30% timeout arm. Require visible retry events/metrics, at least 70% terminal success in each degraded arm, correct terminal error codes for exhausted retries, queue alert Firing in at least one degraded arm, and Resolved after zero-fault recovery. A fast recovered 429 arm is not required to remain backlogged for the alert's full two-minute hold.
5. **Load knee:** use 500 POSTs at concurrency 50 with deterministic 300 ms provider latency, first with one worker and then four. Use a short one-turn prompt, require POST P95 below 150 ms in both arms, and require four-worker queue-lag P95 at least 50% lower. Record single-worker completed throughput rather than inferring it from request creation rate.
6. **SSE reconnect storm:** create 20 short runs and run 20 concurrent clients. Each client closes and reconnects five times with `Last-Event-ID`, then drains the stream. Compare received sequences to PostgreSQL and require no missing or duplicate sequence.

## Evidence and stop lines

Every experiment has a unique run prefix, exact start/end timestamps, run IDs or a content SHA, numeric outcomes, and `ok`. Gate M3 passes only if all six are true, the global `budget_audit` duplicate query in `harness_m3` is empty, and the required normal/429/load-one/load-four 3x4 baseline table is complete. No threshold is relaxed after execution.

Stop and restore immediately if Lab RSS reaches 4 GiB, any non-injected OOM occurs, an unrelated container would be targeted, the normal `harness` database receives an M3-tagged run, or the wrapper cannot prove which Compose service/container it is controlling.

## Post-Gate demo controls

Only after Gate M3 passes, design a separate demo control surface. It may expose allowlisted, auto-recovering scenarios and must never mount the Docker socket into the public API. The post-Gate UI work receives its own tests and acceptance; it cannot retroactively substitute button clicks for the six machine-verifiable experiments.
