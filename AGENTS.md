# Harness Lab agent instructions

This repository is an independent serving-harness experiment. Do not import code, runtime state,
models, databases, queues, caches, logs, or artifacts from the former parent Syncopate repository.

Before making changes, read `README.md`, `docs/harness-lab-plan.md`, `docs/EXPERIMENTS.md`, and the
relevant file under `docs/plans/`. The milestone order and gates in those documents are authoritative.
M0, M1, and M2 are complete. The user explicitly approved M3 on 2026-09-07. Execute only the six
registered experiments through the isolated `harness_m3` runtime and do not describe M3 as passed
until every Gate in its current design and saved evidence is true.

All Lab-owned material must stay under this repository. Commit code, tests, configuration templates,
scripts, and documentation. Never commit `secrets/.env`, models, databases, caches, logs, or raw
artifacts. PostgreSQL is the source of truth; Redis is a disposable transport.

Use tests before implementation changes and record material architecture or gate changes in the
existing docs. Never run destructive gate scripts against the production `harness` database. Remote
operation defaults to `home-5090`; bind service ports to loopback and access them through SSH tunnels.

Do not commit or push unless the user explicitly authorizes it. Stage named paths only and preserve
unrelated user changes.
