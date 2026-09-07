#!/usr/bin/env bash
set -euo pipefail

exec ssh \
  -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18000:127.0.0.1:8000 \
  -L 127.0.0.1:13300:127.0.0.1:3300 \
  home-5090
