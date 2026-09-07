#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lab_root="$(cd "${script_dir}/.." && pwd)"
models_dir="${lab_root}/models/ollama"
log_file="${lab_root}/logs/ollama/server.log"
pid_file="${lab_root}/artifacts/m0/ollama.pid"
ollama_host="127.0.0.1:11434"

if [[ "${1:-}" == "--print-config" ]]; then
  printf '{"models":"%s","log":"%s","pid":"%s","host":"%s"}\n' \
    "${models_dir}" "${log_file}" "${pid_file}" "${ollama_host}"
  exit 0
fi

mkdir -p "${models_dir}" "$(dirname "${log_file}")" "$(dirname "${pid_file}")"

if lsof -nP -iTCP:11434 -sTCP:LISTEN >/dev/null 2>&1; then
  printf 'Refusing to start: TCP port 11434 already has a listener.\n' >&2
  exit 1
fi

server_pid=$$
printf '%s\n' "${server_pid}" >"${pid_file}"
printf 'Started lab-owned Ollama pid=%s host=%s\n' "${server_pid}" "${ollama_host}"
exec env \
  OLLAMA_MODELS="${models_dir}" \
  OLLAMA_HOST="${ollama_host}" \
  ollama serve >>"${log_file}" 2>&1
