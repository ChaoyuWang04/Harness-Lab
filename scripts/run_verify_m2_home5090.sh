#!/usr/bin/env bash
set -euo pipefail

lab_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker_socket=/var/run/docker.sock
docker_cli=/usr/bin/docker
compose_plugin=/usr/libexec/docker/cli-plugins/docker-compose

for required_path in "${docker_socket}" "${docker_cli}" "${compose_plugin}"; do
  if [[ ! -e "${required_path}" ]]; then
    echo "Required runtime-host path is missing: ${required_path}" >&2
    exit 2
  fi
done

if ! docker image inspect harness-lab-api >/dev/null 2>&1; then
  echo "Required verifier image is missing: harness-lab-api" >&2
  exit 2
fi

socket_group="$(stat -c '%g' "${docker_socket}")"

exec docker run --rm \
  --network host \
  --user "$(id -u):$(id -g)" \
  --group-add "${socket_group}" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /usr/bin/docker:/usr/bin/docker:ro \
  -v /usr/libexec/docker/cli-plugins/docker-compose:/usr/libexec/docker/cli-plugins/docker-compose:ro \
  -v "${lab_root}:/workspace" \
  -w /workspace \
  harness-lab-api \
  python scripts/verify_m2.py "$@"
