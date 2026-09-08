#!/usr/bin/env python3
"""Verify Harness Lab cloud credentials without logging secret values."""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.telemetry import normalize_langfuse_base_url


LAB_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = LAB_ROOT / "secrets" / ".env"
DEFAULT_OUTPUT = LAB_ROOT / "artifacts" / "m0" / "cloud_connectivity.json"
REQUIRED_KEYS = (
    "SENTRY_DSN",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
)


def parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def redacted_config(values: dict[str, str]) -> dict[str, bool]:
    return {key: bool(values.get(key)) for key in REQUIRED_KEYS}


def build_sentry_request(dsn: str) -> tuple[urllib.request.Request, str]:
    parsed = urllib.parse.urlsplit(dsn)
    project_id = parsed.path.strip("/")
    public_key = parsed.username or ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not project_id or not public_key:
        raise ValueError("SENTRY_DSN is not a valid project DSN")

    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    endpoint = urllib.parse.urlunsplit(
        (parsed.scheme, host, f"/api/{project_id}/store/", "", "")
    )
    event_id = uuid.uuid4().hex
    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {
        "event_id": event_id,
        "timestamp": timestamp,
        "platform": "python",
        "level": "info",
        "logger": "harness-lab.m0",
        "message": "Harness Lab M0 Sentry connectivity test",
        "tags": {"stage": "M0", "probe": "cloud-connectivity"},
    }
    auth = (
        "Sentry sentry_version=7, "
        f"sentry_client=harness-lab-m0/1.0, sentry_key={public_key}"
    )
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Sentry-Auth": auth},
        method="POST",
    )
    return request, event_id


def build_langfuse_request(
    base_url: str, public_key: str, secret_key: str
) -> urllib.request.Request:
    base_url = normalize_langfuse_base_url(base_url)
    endpoint = base_url.rstrip("/") + "/api/public/projects"
    token = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    return urllib.request.Request(
        endpoint,
        headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
        method="GET",
    )


def _request_json(
    request: urllib.request.Request,
    opener: Any = urllib.request.urlopen,
) -> tuple[int, Any]:
    with opener(request, timeout=30) as response:
        status = int(response.status)
        body = response.read()
    return status, json.loads(body) if body else None


def verify(values: dict[str, str]) -> dict[str, Any]:
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        return {"ok": False, "configured": redacted_config(values), "missing": missing}

    results: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "configured": redacted_config(values),
    }

    try:
        sentry_request, event_id = build_sentry_request(values["SENTRY_DSN"])
        status, _ = _request_json(sentry_request)
        results["sentry"] = {"ok": 200 <= status < 300, "status": status, "event_id": event_id}
    except (ValueError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
        results["sentry"] = {"ok": False, "status": status, "error_type": type(exc).__name__}

    try:
        langfuse_request = build_langfuse_request(
            values["LANGFUSE_BASE_URL"],
            values["LANGFUSE_PUBLIC_KEY"],
            values["LANGFUSE_SECRET_KEY"],
        )
        status, payload = _request_json(langfuse_request)
        projects = payload.get("data", payload) if isinstance(payload, dict) else payload
        project_count = len(projects) if isinstance(projects, list) else None
        results["langfuse"] = {
            "ok": 200 <= status < 300,
            "status": status,
            "project_count": project_count,
        }
    except (ValueError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
        results["langfuse"] = {"ok": False, "status": status, "error_type": type(exc).__name__}

    results["ok"] = bool(results["sentry"]["ok"] and results["langfuse"]["ok"])
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    result = verify(parse_dotenv(args.env_file))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
