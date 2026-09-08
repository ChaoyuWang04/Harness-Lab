#!/usr/bin/env bash
set -euo pipefail

lab_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${lab_root}"

mode="${1:-}"
gate_id="${2:-}"
if [[ "${mode}" == "--restore-only" ]]; then
  gate_id="${2:-gate1}"
fi
if [[ ! "${gate_id}" =~ ^[a-z0-9][a-z0-9_]{0,23}$ ]]; then
  echo "Unsafe gate_id" >&2
  exit 2
fi

generation_database="harness_m4_${gate_id}"
replay_live1_database="harness_m4_replay_live1_${gate_id}"
replay_live2_database="harness_m4_replay_live2_${gate_id}"
test_database="harness_m4_test_${gate_id}"
generation_url="postgresql+psycopg://postgres:harness@postgres:5432/${generation_database}"
replay_live1_url="postgresql+psycopg://postgres:harness@postgres:5432/${replay_live1_database}"
replay_live2_url="postgresql+psycopg://postgres:harness@postgres:5432/${replay_live2_database}"
test_url="postgresql+psycopg://postgres:harness@postgres:5432/${test_database}"
generation_redis="redis://redis:6379/12"
replay_live1_redis="redis://redis:6379/13"
replay_live2_redis="redis://redis:6379/14"
test_redis="redis://redis:6379/15"
artifact_dir="${lab_root}/artifacts/m4/${gate_id}"
compose=(docker compose --env-file secrets/.env --profile m4)
watchdog_pid=""
finalize_requested=false
commit_sha=""

stop_watchdog() {
  if [[ -n "${watchdog_pid}" ]] && kill -0 "${watchdog_pid}" >/dev/null 2>&1; then
    kill "${watchdog_pid}" >/dev/null 2>&1 || true
    wait "${watchdog_pid}" >/dev/null 2>&1 || true
  fi
}

converge_normal_runtime() {
  docker compose --env-file secrets/.env up -d lgtm
  HARNESS_COMPOSE_DATABASE_URL="postgresql+psycopg://postgres:harness@postgres:5432/harness" \
  HARNESS_COMPOSE_REDIS_URL="redis://redis:6379/0" \
  HARNESS_COMPOSE_LLM_BASE_URL="http://ollama:11434/v1" \
  HARNESS_API_WORKERS=4 \
  HARNESS_OTEL_EXPORT_ENABLED=true \
  CAPTURE_MODEL_TURNS=false \
  HARNESS_TEST_PAUSE_AFTER_TOOL_SECONDS=0 \
    docker compose --env-file secrets/.env up -d --force-recreate --scale worker=1 api dispatcher worker sweeper
  docker compose --env-file secrets/.env --profile m4 stop chaos-proxy >/dev/null 2>&1 || true
}

restore_normal_runtime() {
  stop_watchdog
  converge_normal_runtime
  if [[ -d "${artifact_dir}" ]]; then
    docker compose --env-file secrets/.env ps --format json > "${artifact_dir}/post_restore_${mode#--}.json"
    health_ok=false
    for _ in $(seq 1 60); do
      if curl -fsS "http://127.0.0.1:8000/health" > "${artifact_dir}/post_restore_health_${mode#--}.json"; then
        health_ok=true
        break
      fi
      sleep 1
    done
    if [[ "${health_ok}" != "true" ]]; then
      echo "Normal API did not recover" >&2
      return 1
    fi
    container_ids="$(docker compose --env-file secrets/.env ps -q api dispatcher worker sweeper)"
    if [[ -n "${container_ids}" ]]; then
      docker inspect ${container_ids} | python3 -c '
import json, sys
allowed = {"DATABASE_URL", "REDIS_URL", "LLM_BASE_URL", "CAPTURE_MODEL_TURNS", "HARNESS_API_WORKERS"}
result = []
for item in json.load(sys.stdin):
    environment = dict(value.split("=", 1) for value in item["Config"].get("Env", []) if "=" in value)
    result.append({
        "service": item["Config"].get("Labels", {}).get("com.docker.compose.service"),
        "running": bool(item.get("State", {}).get("Running")),
        "environment": {key: environment[key] for key in sorted(allowed) if key in environment},
    })
json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
sys.stdout.write("\n")
' > "${artifact_dir}/post_restore_inspect_${mode#--}.json"
    fi
    if [[ "${finalize_requested}" == "true" ]]; then
      docker run --rm -v "${lab_root}:${lab_root}" -w "${lab_root}" harness-lab-api \
        python -m scripts.verify_m4 finalize \
          --gate-id "${gate_id}" \
          --artifact-dir "${artifact_dir}" \
          --commit "${commit_sha}"
    fi
  fi
}

if [[ "${mode}" == "--restore-only" ]]; then
  restore_normal_runtime
  exit 0
fi
if [[ "${mode}" != "new" && "${mode}" != "resume" ]]; then
  echo "Usage: $0 new|resume|--restore-only GATE_ID" >&2
  exit 2
fi
trap restore_normal_runtime EXIT

mkdir -p "${artifact_dir}"
if [[ "${mode}" == "new" ]]; then
  if find "${artifact_dir}" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "New gate artifact directory is not empty" >&2
    exit 2
  fi
  docker compose --env-file secrets/.env ps --format json > "${artifact_dir}/pre_gate_runtime.json"
else
  if [[ ! -e "${artifact_dir}/gate_state.json" ]]; then
    echo "Resume requires an existing gate_state.json" >&2
    exit 2
  fi
  if [[ -e "${artifact_dir}/pre_resume_runtime.json" ]]; then
    echo "Resume evidence already exists; use a new gate id after a failed resume" >&2
    exit 2
  fi
  docker compose --env-file secrets/.env ps --format json > "${artifact_dir}/pre_resume_runtime.json"
fi

# Always converge stale M4 writers to the ordinary runtime before a new or resumed Gate.
converge_normal_runtime

"${compose[@]}" build \
  --build-arg PACKAGE_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg INSTALL_DEV=true \
  api dispatcher worker sweeper migrate chaos-proxy
docker run --rm harness-lab-api python -c "from app.eval.model_turns import record_model_turn; from app.eval.dataset import build_dataset"
docker compose --env-file secrets/.env up -d postgres redis lgtm ollama
docker compose --env-file secrets/.env stop lgtm
python3 scripts/m4_watchdog.py \
  --output "${artifact_dir}/resource_watchdog.jsonl" \
  --parent "$$" \
  --limit-bytes 4294967296 \
  --interval-seconds 2 &
watchdog_pid="$!"

database_exists() {
  local database_name="$1"
  docker compose --env-file secrets/.env exec -T postgres \
    psql -U postgres -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${database_name}'" | grep -q 1
}

for database_name in "${generation_database}" "${replay_live1_database}" "${replay_live2_database}" "${test_database}"; do
  if [[ "${mode}" == "new" ]]; then
    if ! database_exists "${database_name}"; then
      docker compose --env-file secrets/.env exec -T postgres \
        psql -U postgres -d postgres -c "CREATE DATABASE ${database_name}"
    fi
    table_count="$(docker compose --env-file secrets/.env exec -T postgres \
      psql -U postgres -d "${database_name}" -tAc "SELECT count(*) FROM pg_tables WHERE schemaname='public'")"
    if [[ "${table_count}" -ne 0 ]]; then
      echo "New gate database is not empty: ${database_name}" >&2
      exit 2
    fi
  elif ! database_exists "${database_name}"; then
    echo "Resume database is missing: ${database_name}" >&2
    exit 2
  fi
done

if [[ "${mode}" == "new" ]]; then
  for redis_db in 12 13 14 15; do
    if [[ "${redis_db}" -eq 0 ]]; then exit 2; fi
    docker compose --env-file secrets/.env exec -T redis redis-cli -n "${redis_db}" FLUSHDB >/dev/null
  done
  for database_url in "${generation_url}" "${replay_live1_url}" "${replay_live2_url}" "${test_url}"; do
    HARNESS_COMPOSE_DATABASE_URL="${database_url}" "${compose[@]}" run --rm migrate
  done
  docker run --rm --network harness-lab_default --env-file secrets/.env \
    -e TEST_DATABASE_URL="${test_url}" \
    -e TEST_REDIS_URL="${test_redis}" \
    -v "${lab_root}:${lab_root}" \
    -w "${lab_root}" \
    harness-lab-api python -m pytest -q "${lab_root}/tests"
fi

control_token="$(openssl rand -hex 32)"
commit_sha="$(git rev-parse HEAD)"
HARNESS_COMPOSE_DATABASE_URL="${generation_url}" \
HARNESS_COMPOSE_REDIS_URL="${generation_redis}" \
HARNESS_COMPOSE_LLM_BASE_URL="http://chaos-proxy:9000/v1" \
HARNESS_API_WORKERS=1 \
HARNESS_OTEL_EXPORT_ENABLED=false \
HARNESS_M4_EVAL_MODE=true \
HARNESS_M4_CONTROL_TOKEN="${control_token}" \
CAPTURE_MODEL_TURNS=true \
HARNESS_TEST_PAUSE_AFTER_TOOL_SECONDS=45 \
  "${compose[@]}" up -d --force-recreate --scale worker=1 chaos-proxy api dispatcher worker sweeper

if docker run --rm --network harness-lab_default --env-file secrets/.env \
  -e DATABASE_URL="${generation_url}" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /usr/bin/docker:/usr/bin/docker:ro \
  -v /usr/libexec/docker/cli-plugins/docker-compose:/usr/libexec/docker/cli-plugins/docker-compose:ro \
  -v "${lab_root}:${lab_root}" -w "${lab_root}" harness-lab-api \
  python -m scripts.verify_m4 "${mode}" \
    --gate-id "${gate_id}" \
    --artifact-dir "${artifact_dir}" \
    --config-root "${lab_root}/config/eval" \
    --commit "${commit_sha}" \
    --model "qwen3:0.6b" \
    --database-url "${generation_url}" \
    --normal-database-url "postgresql+psycopg://postgres:harness@postgres:5432/harness" \
    --redis-url "${generation_redis}" \
    --normal-redis-url "redis://redis:6379/0" \
    --test-database-url "${test_url}" \
    --test-redis-url "${test_redis}" \
    --replay-live1-database-url "${replay_live1_url}" \
    --replay-live2-database-url "${replay_live2_url}" \
    --replay-live1-database-name "${replay_live1_database}" \
    --replay-live2-database-name "${replay_live2_database}" \
    --replay-live1-redis-url "${replay_live1_redis}" \
    --replay-live2-redis-url "${replay_live2_redis}" \
    --lab-root "${lab_root}" \
    --api-base "http://api:8000" \
    --proxy-base "http://chaos-proxy:9000" \
    --control-token "${control_token}" \
    --eval-output-dir "${lab_root}/eval"; then
  verifier_rc=0
else
  verifier_rc=$?
fi
if [[ "${mode}" == "resume" && "${verifier_rc}" -eq 0 ]]; then
  finalize_requested=true
fi
exit "${verifier_rc}"
