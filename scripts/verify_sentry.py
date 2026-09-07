#!/usr/bin/env python3
"""Emit one opt-in M2 Sentry event without printing or persisting the DSN."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import sentry_sdk


LAB_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = LAB_ROOT / "secrets" / ".env"
DEFAULT_OUTPUT = LAB_ROOT / "artifacts" / "m2" / "sentry_probe.json"


class SentryM2Probe(RuntimeError):
    pass


def parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _scrub(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("headers", None)
    return event


def run_probe(run_id: str, dsn: str, *, sentry: Any = sentry_sdk) -> dict[str, object]:
    sentry.init(
        dsn=dsn,
        traces_sample_rate=0,
        send_default_pii=False,
        before_send=_scrub,
    )
    with sentry.new_scope() as scope:
        scope.set_tag("run_id", run_id)
        event_id = sentry.capture_exception(SentryM2Probe("Harness Lab M2 verification event"))
    sentry.flush(timeout=10)
    return {"event_id": str(event_id) if event_id else None, "run_id": run_id, "sent": bool(event_id)}


def main() -> int:
    if os.getenv("HARNESS_ENABLE_SENTRY_PROBE") != "1":
        print("Refusing to emit: set HARNESS_ENABLE_SENTRY_PROBE=1", file=sys.stderr)
        return 2
    values = parse_dotenv(DEFAULT_ENV)
    dsn = values.get("SENTRY_DSN", "")
    if not dsn:
        print("SENTRY_DSN is not configured", file=sys.stderr)
        return 2
    run_id = f"run_sentry_m2_{os.getpid()}"
    result = run_probe(run_id, dsn)
    DEFAULT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["sent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
