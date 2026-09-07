# Harness Lab M0 Environment Implementation Plan

> **For agentic workers:** Execute this plan task-by-task in the current checkout. Keep every lab-owned file, model, cache, secret, log, database, and artifact under `harness-lab/`. Do not commit, tag, or enter M1 without the user's explicit approval.

**Goal:** Establish an isolated, reproducible Mac environment and verify that the selected Ollama model can make valid tool calls before any serving application code is written.

**Architecture:** `harness-lab/` is a self-contained project boundary. Source and durable documentation are tracked; secrets, downloaded models, service data, caches, logs, and generated evidence remain inside the boundary but are ignored by Git. Ollama is launched as a lab-owned process with `OLLAMA_MODELS`, `OLLAMA_HOST`, PID, and logs pointing into the lab; later Docker services will use bind mounts under `harness-lab/data/` and explicitly load `harness-lab/secrets/.env`.

**Tech Stack:** macOS, Python 3.12, Ollama, Qwen3 0.6B or fallback 1.7B, Docker Desktop, jq, hey, shell verification scripts.

---

### Task 1: Establish the isolated lab boundary

**Files:**
- Create: `harness-lab/README.md`
- Create: `harness-lab/.gitignore`
- Create: `harness-lab/config/.env.example`
- Create: `harness-lab/docs/EXPERIMENTS.md`
- Create directories: `harness-lab/config/`, `logs/`, `models/`, `scripts/`, `secrets/`, `tests/`, `artifacts/`, `cache/`

- [x] Document the lab-only storage rule and M0/M1 boundary.
- [x] Ignore `secrets/.env`, models, data, caches, logs, and generated artifacts.
- [x] Do not create M1/M4 application, web, database, or evaluation scaffolding during M0.
- [x] Verify no generated or secret path points outside `harness-lab/`.
- [x] Record that later Compose files must use `env_file: ./secrets/.env`; the source plan's root `.env` path is superseded by this isolation layout.

### Task 2: Inventory the host without changing it

**Evidence:**
- Create: `harness-lab/artifacts/m0/host_inventory.txt`

- [x] Record macOS and architecture.
- [x] Record presence and versions of Python, Homebrew, Ollama, Docker, Docker Compose, jq, hey, and curl.
- [x] Record Docker daemon availability and allocated memory.
- [x] Classify missing dependencies; do not install until the inventory is reviewed.
- [x] Apply the user's 2026-09-07 resource decision: the current roughly 7 GB Docker allocation is the gate; do not increase it to 10 GB. Require `docker info` to report at least 7 GiB and measure M2 observability memory before any later change.

### Task 3: Install only missing local dependencies

**Scope:** Ollama, hey, jq, and Python 3.12 if missing.

- [x] Install only confirmed missing tools.
- [x] Create the Python environment only at `harness-lab/.venv/`; direct Python package and temporary caches to `harness-lab/cache/pip/` and `harness-lab/cache/tmp/`.
- [x] Keep any dependency manifest and lock file under `harness-lab/`; do not use or modify the parent project's environment or lock. (M0 scripts use only the Python standard library, so no manifest is needed yet.)
- [x] Re-run the inventory and record exact versions.
- [x] Do not create or modify dependencies for the parent Syncopate project.

### Task 4: Start isolated Ollama and download the candidate model

**Files and evidence:**
- Models: `harness-lab/models/ollama/`
- Logs: `harness-lab/logs/ollama/`
- PID evidence: `harness-lab/artifacts/m0/ollama.pid`
- Create: `harness-lab/scripts/start_ollama.sh`

- [x] Inspect port 11434 ownership before launch; fail rather than reuse an unrelated or default Ollama listener.
- [x] Launch Ollama with explicit `OLLAMA_MODELS`, `OLLAMA_HOST=127.0.0.1:11434`, PID, and log paths under the lab.
- [x] Pull `qwen3:0.6b` into the lab model directory.
- [x] Assert the model directory contains data and the default `~/.ollama/models` was not used for this pull.
- [x] Verify `GET http://localhost:11434/v1/models` returns HTTP 200 and the serving PID matches the recorded lab process.
- [x] Probe `http://host.docker.internal:11434/v1/models` from a temporary container. It returned HTTP 200 without changing the localhost binding or exposing Ollama beyond the host.

### Task 5: Measure tool-calling capability

**Files:**
- Create: `harness-lab/scripts/verify_tool_calling.py`
- Test: `harness-lab/tests/test_verify_tool_calling.py`
- Evidence: `harness-lab/artifacts/m0/tool_calling_qwen3_0.6b.json`

- [x] Write tests for strict validity: response has a tool call, function name is exactly `get_campaign`, arguments parse as JSON, and `campaign_id` equals `camp_001`.
- [x] Run the tests and confirm the validator fails on malformed examples and passes on a valid example.
- [x] Sample the same prompt exactly 20 times at temperature 0 with `/no_think` and no more than three tools.
- [x] Record every raw classification and the aggregate valid-call rate.
- [x] Pass G2 at 70% or higher. If below 70%, stop and present the evidence before downloading the 1.7B fallback. (Final single-variable prompt run: 20/20; no fallback download.)

### Task 6: Prepare cloud observability credentials without integrating SDKs

**Files:**
- Template: `harness-lab/config/.env.example`
- User-owned secret file: `harness-lab/secrets/.env`

- [x] Ask the user to create one Sentry Cloud Python/FastAPI project and place only `SENTRY_DSN` in the secret file.
- [x] Ask the user to create one Langfuse Cloud project and place `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and the selected regional `LANGFUSE_BASE_URL` in the secret file.
- [x] Keep placeholders only in `.env.example`; after the user's explicit request, test the credentials through a redacting probe that records only status, project count, and Sentry event ID—never secret values.
- [x] Do not install or initialize the Sentry or Langfuse SDK before M2.

### Task 7: Record and review Gate M0

**Files:**
- Modify: `harness-lab/docs/EXPERIMENTS.md`
- Evidence: `harness-lab/artifacts/m0/gate_m0.json`

- [x] Record the command, pre-run prediction, actual result, and PASS/FAIL for G1 endpoint health, G2 tool-call success rate, G3 Docker memory, and cloud connectivity.
- [x] Verify secrets are absent from tracked files and evidence.
- [x] Run `git status --short -- harness-lab` and inspect every created path.
- [x] Stop after M0. The user reviewed the resource result and conditionally approved entering M1 once Ollama, Sentry, and Langfuse connectivity passed; all three passed on 2026-09-07.
