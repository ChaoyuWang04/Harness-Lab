from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


UNITS = {
    "B": 1,
    "kB": 1000,
    "KB": 1000,
    "KiB": 1024,
    "MB": 1000**2,
    "MiB": 1024**2,
    "GB": 1000**3,
    "GiB": 1024**3,
}


def parse_bytes(value: str) -> int:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*", value)
    if match is None or match.group(2) not in UNITS:
        raise ValueError(f"unsupported Docker memory value: {value}")
    return round(float(match.group(1)) * UNITS[match.group(2)])


def _command(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def sample() -> dict[str, Any]:
    ids = [
        line
        for line in _command(
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.project=harness-lab",
            "--format",
            "{{.ID}}",
        ).splitlines()
        if line
    ]
    containers = []
    for container_id in ids:
        stat = json.loads(
            _command("docker", "stats", "--no-stream", "--format", "{{json .}}", container_id)
        )
        inspection = json.loads(_command("docker", "inspect", container_id))[0]
        usage = stat["MemUsage"].split("/", 1)[0].strip()
        containers.append(
            {
                "id": inspection["Id"],
                "name": inspection["Name"].lstrip("/"),
                "rss_bytes": parse_bytes(usage),
                "oom_killed": bool(inspection["State"].get("OOMKilled")),
                "restart_count": int(inspection.get("RestartCount", 0)),
            }
        )
    containers.sort(key=lambda item: item["name"])
    return {
        "timestamp_ns": time.time_ns(),
        "rss_bytes": sum(item["rss_bytes"] for item in containers),
        "oom_count": sum(item["oom_killed"] for item in containers),
        "restart_count": sum(item["restart_count"] for item in containers),
        "containers": containers,
    }


def append_sample(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description="Append-only M4 container resource watchdog")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--limit-bytes", type=int, required=True)
    parser.add_argument("--interval-seconds", type=float, default=2)
    args = parser.parse_args()
    stopping = False

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        try:
            current = sample()
        except Exception as error:
            current = {
                "timestamp_ns": time.time_ns(),
                "sample_error": type(error).__name__,
                "rss_bytes": -1,
            }
        append_sample(args.output, current)
        if current.get("rss_bytes", -1) >= args.limit_bytes:
            os.kill(args.parent, signal.SIGTERM)
            return
        if stopping or os.getppid() == 1:
            return
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
