#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  printf 'Run with sudo: sudo %s\n' "$0" >&2
  exit 1
fi

if [[ ! -r /etc/os-release ]]; then
  printf 'Cannot identify operating system.\n' >&2
  exit 1
fi

. /etc/os-release
if [[ "${ID}" != "ubuntu" || "${VERSION_ID}" != "24.04" ]]; then
  printf 'Expected Ubuntu 24.04, found %s %s.\n' "${ID}" "${VERSION_ID}" >&2
  exit 1
fi

target_user="${SUDO_USER:-}"
if [[ -z "${target_user}" || "${target_user}" == "root" ]]; then
  printf 'Run this script through sudo from the intended non-root operator account.\n' >&2
  exit 1
fi

conflicts=()
for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
  if dpkg-query -W -f='${Status}' "${package}" 2>/dev/null | grep -q 'install ok installed'; then
    conflicts+=("${package}")
  fi
done
if ((${#conflicts[@]})); then
  printf 'Refusing to remove conflicting packages automatically: %s\n' "${conflicts[*]}" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg2

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

architecture="$(dpkg --print-architecture)"
codename="${UBUNTU_CODENAME:-${VERSION_CODENAME}}"
printf '%s\n' \
  'Types: deb' \
  'URIs: https://download.docker.com/linux/ubuntu' \
  "Suites: ${codename}" \
  'Components: stable' \
  "Architectures: ${architecture}" \
  'Signed-By: /etc/apt/keyrings/docker.asc' \
  >/etc/apt/sources.list.d/docker.sources

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  >/etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

nvidia_toolkit_version="1.20.0-1"
apt-get install -y \
  "nvidia-container-toolkit=${nvidia_toolkit_version}" \
  "nvidia-container-toolkit-base=${nvidia_toolkit_version}" \
  "libnvidia-container-tools=${nvidia_toolkit_version}" \
  "libnvidia-container1=${nvidia_toolkit_version}"

nvidia-ctk runtime configure --runtime=docker
systemctl enable --now docker
systemctl restart docker
usermod -aG docker "${target_user}"

printf '\nInstalled versions:\n'
docker version --format 'Docker Engine {{.Server.Version}}'
docker compose version
nvidia-ctk --version
printf 'Added %s to the docker group. Log out and reconnect before non-sudo Docker use.\n' "${target_user}"
