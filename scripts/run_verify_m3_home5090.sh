#!/usr/bin/env bash
set -euo pipefail

lab_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${lab_root}"

gate_id="${1:-gate1}"
verifier_mode_args=()
case "${gate_id}" in
  gate1)
    m3_database_name="harness_m3"
    m3_test_database_name="harness_m3_test"
    m3_redis_url="redis://redis:6379/1"
    m3_output="${lab_root}/artifacts/m3/gate_m3.json"
    ;;
  gate2)
    m3_database_name="harness_m3_gate2"
    m3_test_database_name="harness_m3_gate2_test"
    m3_redis_url="redis://redis:6379/2"
    m3_output="${lab_root}/artifacts/m3/gate2/gate_m3.json"
    ;;
  gate3)
    m3_database_name="harness_m3_gate3"
    m3_test_database_name="harness_m3_gate3_test"
    m3_redis_url="redis://redis:6379/3"
    m3_output="${lab_root}/artifacts/m3/gate3/gate_m3.json"
    ;;
  gate4)
    m3_database_name="harness_m3_gate4"
    m3_test_database_name="harness_m3_gate4_test"
    m3_redis_url="redis://redis:6379/4"
    m3_output="${lab_root}/artifacts/m3/gate4/gate_m3.json"
    ;;
  gate5)
    m3_database_name="harness_m3_gate5"
    m3_test_database_name="harness_m3_gate5_test"
    m3_redis_url="redis://redis:6379/5"
    m3_output="${lab_root}/artifacts/m3/gate5/gate_m3.json"
    ;;
  gate6)
    m3_database_name="harness_m3_gate6"
    m3_test_database_name="harness_m3_gate6_test"
    m3_redis_url="redis://redis:6379/6"
    m3_output="${lab_root}/artifacts/m3/gate6/gate_m3.json"
    ;;
  gate7)
    m3_database_name="harness_m3_gate7"
    m3_test_database_name="harness_m3_gate7_test"
    m3_redis_url="redis://redis:6379/7"
    m3_output="${lab_root}/artifacts/m3/gate7/gate_m3.json"
    ;;
  gate8)
    m3_database_name="harness_m3_gate8"
    m3_test_database_name="harness_m3_gate8_test"
    m3_redis_url="redis://redis:6379/8"
    m3_output="${lab_root}/artifacts/m3/gate8/gate_m3.json"
    ;;
  gate9)
    m3_database_name="harness_m3_gate9"
    m3_test_database_name="harness_m3_gate9_test"
    m3_redis_url="redis://redis:6379/11"
    m3_output="${lab_root}/artifacts/m3/gate9/gate_m3.json"
    ;;
  poolwarm_probe)
    m3_database_name="harness_m3_poolwarm_probe"
    m3_test_database_name="harness_m3_poolwarm_probe_test"
    m3_redis_url="redis://redis:6379/9"
    m3_output="${lab_root}/artifacts/m3/diagnostics/load_four_poolwarm.json"
    verifier_mode_args=(--load-four-only)
    ;;
  writepath_probe)
    m3_database_name="harness_m3_writepath_probe"
    m3_test_database_name="harness_m3_writepath_probe_test"
    m3_redis_url="redis://redis:6379/10"
    m3_output="${lab_root}/artifacts/m3/diagnostics/load_four_writepath.json"
    verifier_mode_args=(--load-four-only)
    ;;
  *)
    echo "Unsupported M3 gate id: ${gate_id}" >&2
    exit 2
    ;;
esac
m3_database_url="postgresql+psycopg://postgres:harness@postgres:5432/${m3_database_name}"
m3_test_database_url="postgresql+psycopg://postgres:harness@postgres:5432/${m3_test_database_name}"
m3_llm_url="http://chaos-proxy:9000/v1"
compose=(docker compose --env-file secrets/.env --profile m3)

restore_normal_runtime() {
  docker compose --env-file secrets/.env up -d redis
  docker compose --env-file secrets/.env up -d --force-recreate --scale worker=1 api dispatcher worker sweeper
  docker compose --env-file secrets/.env --profile m3 stop chaos-proxy >/dev/null 2>&1 || true
}
trap restore_normal_runtime EXIT

"${compose[@]}" build api dispatcher worker sweeper migrate chaos-proxy
docker run --rm harness-lab-dispatcher python -c \
  "from app.chaos.hooks import crash_after_publish_once"
docker run --rm \
  -v "${lab_root}/data/chaos:/var/lib/harness-chaos" \
  harness-lab-api \
  python -c "from pathlib import Path; Path('/var/lib/harness-chaos/dispatcher-after-publish.once').unlink(missing_ok=True)"
docker compose --env-file secrets/.env exec -T postgres sh -c \
  "psql -U postgres -d postgres -tAc \"SELECT 1 FROM pg_database WHERE datname='${m3_database_name}'\" | grep -q 1 || psql -U postgres -d postgres -c \"CREATE DATABASE ${m3_database_name}\""
docker compose --env-file secrets/.env exec -T postgres sh -c \
  "psql -U postgres -d postgres -tAc \"SELECT 1 FROM pg_database WHERE datname='${m3_test_database_name}'\" | grep -q 1 || psql -U postgres -d postgres -c \"CREATE DATABASE ${m3_test_database_name}\""
redis_database="${m3_redis_url##*/}"
docker compose --env-file secrets/.env exec -T redis redis-cli -n "${redis_database}" FLUSHDB >/dev/null

HARNESS_COMPOSE_DATABASE_URL="${m3_database_url}" \
  "${compose[@]}" run --rm migrate
HARNESS_COMPOSE_DATABASE_URL="${m3_test_database_url}" \
  "${compose[@]}" run --rm migrate

docker run --rm \
  --network harness-lab_default \
  -e TEST_DATABASE_URL="${m3_test_database_url}" \
  -e TEST_REDIS_URL="${m3_redis_url}" \
  -v "${lab_root}:${lab_root}" \
  -w "${lab_root}" \
  harness-lab-api \
  python tests/test_outbox.py

docker run --rm \
  --network harness-lab_default \
  -e TEST_DATABASE_URL="${m3_test_database_url}" \
  -v "${lab_root}:${lab_root}" \
  -w "${lab_root}" \
  harness-lab-api \
  python tests/test_runs_service.py

HARNESS_COMPOSE_DATABASE_URL="${m3_database_url}" \
HARNESS_COMPOSE_REDIS_URL="${m3_redis_url}" \
HARNESS_COMPOSE_LLM_BASE_URL="${m3_llm_url}" \
  "${compose[@]}" up -d --force-recreate --scale worker=1 chaos-proxy api dispatcher worker sweeper

docker network inspect harness-lab_default >/dev/null
socket_gid="$(stat -c '%g' /var/run/docker.sock)"
current_uid="$(id -u)"
current_gid="$(id -g)"

docker run --rm \
  --network harness-lab_default \
  --user "${current_uid}:${current_gid}" \
  --group-add "${socket_gid}" \
  -e HARNESS_COMPOSE_DATABASE_URL="${m3_database_url}" \
  -e HARNESS_COMPOSE_REDIS_URL="${m3_redis_url}" \
  -e HARNESS_COMPOSE_LLM_BASE_URL="${m3_llm_url}" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /usr/bin/docker:/usr/bin/docker:ro \
  -v /usr/libexec/docker/cli-plugins/docker-compose:/usr/libexec/docker/cli-plugins/docker-compose:ro \
  -v "${lab_root}:${lab_root}" \
  -w "${lab_root}" \
  harness-lab-api \
  python scripts/verify_m3.py \
    "${verifier_mode_args[@]}" \
    --api-base http://api:8000 \
    --grafana-base http://lgtm:3000 \
    --proxy-base http://chaos-proxy:9000 \
    --database-url "${m3_database_url}" \
    --normal-database-url postgresql+psycopg://postgres:harness@postgres:5432/harness \
    --redis-url "${m3_redis_url}" \
    --output "${m3_output}"
