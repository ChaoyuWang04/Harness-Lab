#!/usr/bin/env bash
set -euo pipefail

lab_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${lab_root}"

probe_database="harness_m3_api4_probe4"
probe_database_url="postgresql+psycopg://postgres:harness@postgres:5432/${probe_database}"
probe_output="${lab_root}/artifacts/m3/diagnostics/api4_capacity_probe4.json"
compose=(docker compose --env-file secrets/.env)

restore_normal_api() {
  "${compose[@]}" up -d --force-recreate api
}
trap restore_normal_api EXIT

"${compose[@]}" build api migrate
"${compose[@]}" exec -T postgres sh -c \
  "psql -U postgres -d postgres -tAc \"SELECT 1 FROM pg_database WHERE datname='${probe_database}'\" | grep -q 1 || psql -U postgres -d postgres -c \"CREATE DATABASE ${probe_database}\""
HARNESS_COMPOSE_DATABASE_URL="${probe_database_url}" \
  "${compose[@]}" run --rm migrate

existing_runs="$("${compose[@]}" exec -T postgres psql -U postgres -d "${probe_database}" -Atc "SELECT count(*) FROM agent_runs")"
if [[ "${existing_runs}" != "0" ]]; then
  echo "Refusing to reuse non-empty API capacity probe database" >&2
  exit 2
fi

HARNESS_COMPOSE_DATABASE_URL="${probe_database_url}" \
HARNESS_COMPOSE_REDIS_URL="redis://redis:6379/15" \
  "${compose[@]}" up -d --force-recreate api

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:8000/health >/dev/null
mkdir -p "$(dirname "${probe_output}")"

current_uid="$(id -u)"
current_gid="$(id -g)"
docker run --rm \
  --network harness-lab_default \
  --user "${current_uid}:${current_gid}" \
  -v "${lab_root}:${lab_root}" \
  -w "${lab_root}" \
  harness-lab-api \
  python scripts/verify_m3.py \
    --api-capacity-only \
    --api-base http://api:8000 \
    --database-url "${probe_database_url}" \
    --normal-database-url postgresql+psycopg://postgres:harness@postgres:5432/harness \
    --redis-url redis://redis:6379/15 \
    --output "${probe_output}"
