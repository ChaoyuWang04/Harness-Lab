#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lab_root="$(cd "${script_dir}/.." && pwd)"
cd "${lab_root}"

docker compose --env-file secrets/.env config --quiet
docker compose ps

for port in 8000 3300; do
  if ! ss -ltn "sport = :${port}" | grep -q '127.0.0.1'; then
    printf 'Port %s is not bound to server loopback.\n' "${port}" >&2
    exit 1
  fi
done

for port in 5432 6379 4317 4318 11434; do
  if ss -ltn "sport = :${port}" | grep -q 'LISTEN'; then
    printf 'Internal port %s is unexpectedly published on the host.\n' "${port}" >&2
    exit 1
  fi
done

curl -fsS http://127.0.0.1:8000/health >/dev/null
curl -fsS http://127.0.0.1:3300/api/health >/dev/null
docker compose exec -T ollama nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
docker compose exec -T ollama ollama list

printf 'home-5090 Harness Lab runtime verification passed.\n'
