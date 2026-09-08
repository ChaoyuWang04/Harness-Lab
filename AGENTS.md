# Harness Lab agent instructions

This repository is an independent serving-harness experiment. Do not import code, runtime state,
models, databases, queues, caches, logs, or artifacts from the former parent Syncopate repository.

Before making changes, read `README.md`, `docs/harness-lab-plan.md`, `docs/EXPERIMENTS.md`, and the
relevant file under `docs/plans/`. The milestone order and gates in those documents are authoritative.
M0 through M3 are complete. The next approved line is the expanded M4 trajectory-to-evaluation
flywheel in `docs/harness-lab-plan.md`; it must pass before the L0-L8 disaster-observation learning
platform in `docs/learn-platform-plan.md` starts. The Learn platform's 17 software-fault scenarios,
observability gates, and runbooks must pass before its course/exam UI work. Keep one frontend debug
console: extend the existing `web/index.html` with in-page modes; do not add a second frontend entry,
port, or standalone app.

All Lab-owned material must stay under this repository. Commit code, tests, configuration templates,
scripts, and documentation. Never commit `secrets/.env`, models, databases, caches, logs, or raw
artifacts. PostgreSQL is the source of truth; Redis is a disposable transport.

Use tests before implementation changes and record material architecture or gate changes in the
existing docs. Never run destructive gate scripts against the production `harness` database. Remote
operation defaults to `home-5090`; bind service ports to loopback and access them through SSH tunnels.

Do not commit or push unless the user explicitly authorizes it. Stage named paths only and preserve
unrelated user changes.
