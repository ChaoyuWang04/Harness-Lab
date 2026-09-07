#!/usr/bin/env python3
"""Exercise the queue-lag alert through Normal, Firing, and recovery."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LAB_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = LAB_ROOT / "artifacts" / "m2" / "alert_verified.json"
TERMINAL = {"completed", "failed", "cancelled"}


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _json_request(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> Any:
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with _opener().open(request, timeout=30) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


def _grafana_headers() -> dict[str, str]:
    token = base64.b64encode(b"admin:admin").decode("ascii")
    return {"Authorization": f"Basic {token}"}


def queue_age(grafana_base: str) -> float:
    query = urllib.parse.urlencode({"query": "max(agent_oldest_queued_age_seconds)"})
    payload = _json_request(
        f"{grafana_base}/api/datasources/proxy/uid/prometheus/api/v1/query?{query}",
        headers=_grafana_headers(),
    )
    results = payload.get("data", {}).get("result", [])
    if not results:
        raise AssertionError("queue age metric has no data")
    return float(results[0]["value"][1])


def active_queue_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        alert
        for alert in alerts
        if alert.get("labels", {}).get("alertname") == "Harness queue lag"
        and alert.get("status", {}).get("state") == "active"
    ]


def read_active_queue_alerts(grafana_base: str) -> list[dict[str, Any]]:
    alerts = _json_request(
        f"{grafana_base}/api/alertmanager/grafana/api/v2/alerts",
        headers=_grafana_headers(),
    )
    return active_queue_alerts(alerts)


def validate_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    negative = evidence["negative"]
    positive = evidence["positive"]
    recovery = evidence["recovery"]
    ok = (
        float(negative["duration_seconds"]) >= 120
        and float(negative["max_value"]) <= 10
        and int(negative["active_alerts"]) == 0
        and float(positive["max_value"]) > 10
        and bool(positive["firing"])
        and int(recovery["active_alerts"]) == 0
        and recovery["run_status"] in TERMINAL
    )
    if not ok:
        raise AssertionError("alert evidence does not cover Normal, Firing, and recovery")
    return {"ok": True, **evidence}


def _wait_run(api_base: str, run_id: str, timeout_seconds: float) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = _json_request(f"{api_base}/runs/{run_id}")["status"]
        if status in TERMINAL:
            return status
        time.sleep(2)
    return "timeout"


def verify(api_base: str, grafana_base: str) -> dict[str, Any]:
    negative_started = time.monotonic()
    negative_values: list[float] = []
    while time.monotonic() - negative_started < 120:
        value = queue_age(grafana_base)
        if value > 10 or read_active_queue_alerts(grafana_base):
            raise AssertionError("negative control left Normal state")
        negative_values.append(value)
        print(f"negative_seconds={int(time.monotonic() - negative_started)}", flush=True)
        time.sleep(10)
    negative_duration = time.monotonic() - negative_started

    run_id = ""
    firing_alerts: list[dict[str, Any]] = []
    positive_values: list[float] = []
    subprocess.run(["docker", "compose", "stop", "worker"], cwd=LAB_ROOT, check=True)
    try:
        created = _json_request(
            f"{api_base}/runs",
            method="POST",
            body={"prompt": "诊断 camp_001 今日消耗并汇报预算使用率"},
            headers={"Idempotency-Key": f"m2-alert-{datetime.now(timezone.utc).isoformat()}"},
        )
        run_id = created["run_id"]
        deadline = time.monotonic() + 210
        while time.monotonic() < deadline:
            value = queue_age(grafana_base)
            positive_values.append(value)
            firing_alerts = read_active_queue_alerts(grafana_base)
            print(f"positive_queue_age={value:.1f} firing={bool(firing_alerts)}", flush=True)
            if value > 10 and firing_alerts:
                break
            time.sleep(5)
        if not firing_alerts:
            raise AssertionError("queue-lag alert did not enter Firing")
    finally:
        subprocess.run(["docker", "compose", "start", "worker"], cwd=LAB_ROOT, check=True)

    run_status = _wait_run(api_base, run_id, 120)
    recovery_deadline = time.monotonic() + 90
    recovered_alerts = firing_alerts
    while time.monotonic() < recovery_deadline:
        recovered_alerts = read_active_queue_alerts(grafana_base)
        if not recovered_alerts:
            break
        time.sleep(5)

    return validate_evidence(
        {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "negative": {
                "duration_seconds": round(negative_duration, 3),
                "max_value": max(negative_values),
                "active_alerts": 0,
                "sample_count": len(negative_values),
            },
            "positive": {
                "run_id": run_id,
                "max_value": max(positive_values),
                "firing": bool(firing_alerts),
                "sample_count": len(positive_values),
            },
            "recovery": {
                "active_alerts": len(recovered_alerts),
                "run_status": run_status,
            },
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--grafana-base", default="http://127.0.0.1:3300")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    evidence = verify(args.api_base, args.grafana_base)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": evidence["ok"], "evidence": str(args.output)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
