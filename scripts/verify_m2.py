#!/usr/bin/env python3
"""Run the sole 30-run M2 cohort and write redacted gate evidence."""

from __future__ import annotations

import argparse
import base64
import json
import platform
import subprocess
import time
import urllib.parse
import urllib.request
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LAB_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = LAB_ROOT / "artifacts" / "m2" / "gate_m2.json"
DEFAULT_ENV = LAB_ROOT / "secrets" / ".env"
TERMINAL = {"completed", "failed", "cancelled"}
REQUIRED_SERVICES = frozenset(
    {"api", "dispatcher", "lgtm", "ollama", "postgres", "redis", "sweeper", "worker"}
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


def validate_cohort(runs: list[dict[str, Any]]) -> dict[str, Any]:
    if len(runs) != 30 or len({run["run_id"] for run in runs}) != 30:
        raise AssertionError("cohort must contain exactly 30 unique runs")
    if any(run.get("status") not in TERMINAL for run in runs):
        raise AssertionError("all cohort runs must be terminal")
    return {"ok": True, "count": 30, "completed": sum(run["status"] == "completed" for run in runs)}


def validate_panel_results(values: dict[str, Any]) -> dict[str, Any]:
    required = ("run_p95_seconds", "failed_rate", "queue_lag_p95_seconds", "model_or_tool_series")
    if any(name not in values or values[name] is None for name in required):
        raise AssertionError("four dashboard panel results are required")
    if int(values["model_or_tool_series"]) < 1:
        raise AssertionError("four dashboard panel results must be non-empty")
    for name in required[:3]:
        if not isinstance(values[name], (int, float)):
            raise AssertionError(f"{name} must be numeric")
        if not math.isfinite(float(values[name])):
            raise AssertionError(f"{name} must be finite")
    return {"ok": True, **values}


def validate_resources(values: dict[str, Any]) -> dict[str, Any]:
    if int(values.get("docker_mem_total_bytes", 0)) <= 0:
        raise AssertionError("Docker memory must be measured on the runtime host")
    running_services = set(values.get("running_services", []))
    missing_services = sorted(REQUIRED_SERVICES - running_services)
    if missing_services:
        raise AssertionError(f"missing required running services: {', '.join(missing_services)}")
    if float(values.get("aggregate_rss_mib", 4096)) >= 4096:
        raise AssertionError("aggregate RSS must remain below 4 GiB")
    if int(values.get("oom_count", 1)) != 0:
        raise AssertionError("OOM count must be zero")
    if int(values.get("unexpected_restart_count", 1)) != 0:
        raise AssertionError("unexpected restart count must be zero")
    return {"ok": True, **values}


def write_redacted_evidence(path: Path, evidence: dict[str, Any], secrets: list[str]) -> None:
    serialized = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if any(secret and secret in serialized for secret in secrets):
        raise AssertionError("secret value found in gate evidence")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _json_request(url: str, *, method: str = "GET", body: Any = None, headers: dict[str, str] | None = None) -> Any:
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with _opener().open(request, timeout=30) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


def run_cohort(api_base: str) -> tuple[list[dict[str, Any]], str, str]:
    started = datetime.now(timezone.utc).isoformat()
    run_ids = []
    for index in range(30):
        created = _json_request(
            f"{api_base}/runs",
            method="POST",
            body={"prompt": "诊断 camp_001 今日消耗并汇报预算使用率"},
            headers={"Idempotency-Key": f"m2-cohort-{started}-{index}"},
        )
        run_ids.append(created["run_id"])

    deadline = time.monotonic() + 600
    terminal: dict[str, dict[str, Any]] = {}
    while len(terminal) < 30 and time.monotonic() < deadline:
        for run_id in run_ids:
            if run_id in terminal:
                continue
            result = _json_request(f"{api_base}/runs/{run_id}")
            if result["status"] in TERMINAL:
                terminal[run_id] = result
        if len(terminal) < 30:
            time.sleep(1)
    runs = [terminal.get(run_id, {"run_id": run_id, "status": "timeout"}) for run_id in run_ids]
    validate_cohort(runs)
    # Keep the cohort window exclusive while allowing the final bounded OTLP
    # export and Prometheus ingestion to settle.
    time.sleep(12)
    return runs, started, datetime.now(timezone.utc).isoformat()


def _grafana_headers() -> dict[str, str]:
    token = base64.b64encode(b"admin:admin").decode("ascii")
    return {"Authorization": f"Basic {token}"}


def prometheus_query(grafana_base: str, expression: str, timestamp: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"query": expression, "time": timestamp})
    payload = _json_request(
        f"{grafana_base}/api/datasources/proxy/uid/prometheus/api/v1/query?{query}",
        headers=_grafana_headers(),
    )
    return payload.get("data", {}).get("result", [])


def _scalar(result: list[dict[str, Any]]) -> float | None:
    if not result:
        return None
    return float(result[0]["value"][1])


def _instance_ids(result: list[dict[str, Any]]) -> set[str]:
    return {
        item.get("metric", {}).get("service_instance_id", "")
        for item in result
        if item.get("metric", {}).get("service_instance_id")
    }


def _sum_new_instances(
    result: list[dict[str, Any]],
    label: str,
    cohort_instances: set[str],
) -> dict[str, float]:
    values: dict[str, float] = {}
    for item in result:
        metric = item.get("metric", {})
        if metric.get("service_instance_id") not in cohort_instances:
            continue
        key = metric.get(label, "")
        values[key] = values.get(key, 0.0) + float(item["value"][1])
    return values


def _histogram_quantile(buckets: dict[str, float], quantile: float) -> float:
    ordered = sorted(
        ((math.inf if boundary == "+Inf" else float(boundary), count) for boundary, count in buckets.items()),
        key=lambda item: item[0],
    )
    if not ordered or not math.isinf(ordered[-1][0]) or ordered[-1][1] <= 0:
        return math.nan
    rank = quantile * ordered[-1][1]
    lower_bound = 0.0
    lower_count = 0.0
    for upper_bound, count in ordered:
        if count >= rank:
            if math.isinf(upper_bound):
                return lower_bound
            if count <= lower_count:
                return upper_bound
            fraction = (rank - lower_count) / (count - lower_count)
            return lower_bound + (upper_bound - lower_bound) * fraction
        lower_bound = upper_bound
        lower_count = count
    return math.nan


def query_panels(
    grafana_base: str,
    started_at: str,
    ended_at: str,
    *,
    expected_run_count: int,
) -> dict[str, Any]:
    dashboard = _json_request(
        f"{grafana_base}/api/dashboards/uid/harness-slo", headers=_grafana_headers()
    )
    if len(dashboard.get("dashboard", {}).get("panels", [])) != 4:
        raise AssertionError("Harness SLO dashboard does not have four panels")
    selector = '{service_name="harness-worker"}'
    expressions = {
        "runs": f"agent_run_total{selector}",
        "duration": f"agent_run_duration_seconds_bucket{selector}",
        "queue_lag": f"agent_queue_lag_seconds_bucket{selector}",
        "model": f"agent_model_call_total{selector}",
        "tools": f"agent_tool_latency_ms_count{selector}",
    }
    run_before = prometheus_query(grafana_base, expressions["runs"], started_at)
    run_after = prometheus_query(grafana_base, expressions["runs"], ended_at)
    cohort_instances = _instance_ids(run_after) - _instance_ids(run_before)
    duration = _sum_new_instances(
        prometheus_query(grafana_base, expressions["duration"], ended_at), "le", cohort_instances
    )
    queue_lag = _sum_new_instances(
        prometheus_query(grafana_base, expressions["queue_lag"], ended_at), "le", cohort_instances
    )
    runs = _sum_new_instances(run_after, "status", cohort_instances)
    model = _sum_new_instances(
        prometheus_query(grafana_base, expressions["model"], ended_at),
        "http_status",
        cohort_instances,
    )
    tools = _sum_new_instances(
        prometheus_query(grafana_base, expressions["tools"], ended_at),
        "tool_name",
        cohort_instances,
    )
    total_runs = sum(runs.values())
    if int(total_runs) != expected_run_count:
        raise AssertionError(
            f"metric run sample count {int(total_runs)} does not match cohort {expected_run_count}"
        )
    values = {
        "run_p95_seconds": _histogram_quantile(duration, 0.95),
        "failed_rate": runs.get("failed", 0.0) / max(total_runs, 1.0),
        "queue_lag_p95_seconds": _histogram_quantile(queue_lag, 0.95),
        "model_or_tool_series": sum(value > 0 for value in model.values())
        + sum(value > 0 for value in tools.values()),
        "run_samples": int(total_runs),
    }
    return validate_panel_results(values)


REQUIRED_TRACE_SPANS = frozenset(
    {
        "harness.api_create_run",
        "harness.dispatch",
        "harness.execute_run",
        "harness.model_call",
        "harness.tool_call",
    }
)


def validate_trace_spans(span_names: set[str]) -> dict[str, Any]:
    missing = sorted(REQUIRED_TRACE_SPANS - span_names)
    if missing:
        raise AssertionError(f"missing required spans: {', '.join(missing)}")
    return {"ok": True, "span_names": sorted(span_names)}


def _collect_span_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        if isinstance(value.get("name"), str) and "spanId" in value:
            names.add(value["name"])
        for child in value.values():
            names.update(_collect_span_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_collect_span_names(child))
    return names


def query_tempo(
    grafana_base: str,
    run_id: str,
    *,
    attempts: int = 12,
    delay_seconds: float = 5.0,
) -> dict[str, Any]:
    traceql = f'{{ span.run.id = "{run_id}" }}'
    query = urllib.parse.urlencode({"q": traceql, "limit": 20})
    for attempt in range(attempts):
        payload = _json_request(
            f"{grafana_base}/api/datasources/proxy/uid/tempo/api/search?{query}",
            headers=_grafana_headers(),
        )
        trace_ids = [item.get("traceID") for item in payload.get("traces", []) if item.get("traceID")]
        for trace_id in trace_ids:
            trace_payload = _json_request(
                f"{grafana_base}/api/datasources/proxy/uid/tempo/api/traces/{trace_id}",
                headers=_grafana_headers(),
            )
            span_names = _collect_span_names(trace_payload)
            try:
                validated = validate_trace_spans(span_names)
            except AssertionError:
                continue
            return {**validated, "trace_id": trace_id, "run_id": run_id}
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    raise AssertionError(f"no single Tempo trace for {run_id} contains all required spans")


def _langfuse_http_client() -> Any:
    import httpx

    # The local verifier must not inherit a flaky desktop proxy for this
    # external read. Service-side export remains governed by its own runtime.
    return httpx.Client(timeout=30, trust_env=False)


def query_langfuse(values: dict[str, str], run_id: str) -> dict[str, Any]:
    from langfuse import Langfuse

    http_client = _langfuse_http_client()
    client = Langfuse(
        public_key=values["LANGFUSE_PUBLIC_KEY"],
        secret_key=values["LANGFUSE_SECRET_KEY"],
        base_url=values["LANGFUSE_BASE_URL"],
        tracing_enabled=False,
        httpx_client=http_client,
    )
    response = client.api.observations.get_many(
        session_id=run_id,
        type="GENERATION",
        limit=100,
        fields="core,basic,io,model,usage,metrics,metadata",
    )
    observations = response.data
    complete = bool(observations)
    rounds = []
    for item in observations:
        usage = item.usage_details or {}
        round_ok = item.input is not None and item.output is not None and all(
            name in usage for name in ("input", "output", "total")
        )
        complete = complete and round_ok
        rounds.append({"id": item.id, "complete": round_ok, "usage": usage})
    client.shutdown()
    http_client.close()
    return {"ok": complete, "round_count": len(rounds), "rounds": rounds}


def query_cohort_observability(
    grafana_base: str,
    values: dict[str, str],
    runs: list[dict[str, Any]],
    *,
    attempts: int = 6,
    delay_seconds: float = 5.0,
) -> dict[str, Any]:
    """Select one cohort run whose trace and generations are both complete."""
    last_errors: list[str] = []
    for attempt in range(attempts):
        last_errors = []
        for run in runs:
            run_id = run["run_id"]
            try:
                tempo = query_tempo(grafana_base, run_id, attempts=1)
            except AssertionError as error:
                last_errors.append(f"{run_id}: {error}")
                continue
            langfuse = query_langfuse(values, run_id)
            if langfuse.get("ok"):
                return {
                    "ok": True,
                    "run_id": run_id,
                    "tempo": tempo,
                    "langfuse": langfuse,
                }
            last_errors.append(f"{run_id}: incomplete Langfuse generations")
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    detail = "; ".join(last_errors[:3])
    raise AssertionError(f"no cohort run has complete Tempo and Langfuse evidence: {detail}")


def validate_sentry_issue(
    probe_path: Path,
    issue_path: Path,
    *,
    lab_root: Path = LAB_ROOT,
) -> dict[str, Any]:
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    issue = json.loads(issue_path.read_text(encoding="utf-8"))
    if not probe.get("sent") or not issue.get("ok"):
        raise AssertionError("Sentry probe and visible issue evidence must both be successful")
    if issue.get("event_id") != probe.get("event_id") or issue.get("run_id") != probe.get("run_id"):
        raise AssertionError("visible Sentry issue does not match the sent probe identity")
    screenshot_value = issue.get("screenshot")
    if not isinstance(screenshot_value, str) or not screenshot_value:
        raise AssertionError("visible Sentry issue evidence must name a screenshot")
    root = lab_root.resolve()
    screenshot = (root / screenshot_value).resolve()
    if root not in screenshot.parents or not screenshot.is_file() or screenshot.stat().st_size == 0:
        raise AssertionError("visible Sentry issue screenshot is missing or outside the Lab")
    return issue


def _memory_to_mib(value: str) -> float:
    number, unit = value.strip().split() if " " in value.strip() else (value[:-3], value[-3:])
    factors = {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "kB": 1 / 1000, "MB": 1, "GB": 1000}
    return float(number) * factors[unit]


def docker_resources(env_file: Path = DEFAULT_ENV) -> dict[str, Any]:
    mem_total = int(subprocess.check_output(["docker", "info", "--format", "{{.MemTotal}}"], text=True).strip())
    stats_text = subprocess.check_output(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"], text=True
    )
    lab_stats = [json.loads(line) for line in stats_text.splitlines() if "harness-lab-" in line]
    rss = sum(_memory_to_mib(item["MemUsage"].split("/")[0].strip()) for item in lab_stats)
    compose = ["docker", "compose", "--env-file", str(env_file)]
    running_services = subprocess.check_output(
        [*compose, "ps", "--services", "--status", "running"], cwd=LAB_ROOT, text=True
    ).split()
    ids = subprocess.check_output([*compose, "ps", "-q"], cwd=LAB_ROOT, text=True).split()
    oom = 0
    restarts = 0
    for container_id in ids:
        state = json.loads(
            subprocess.check_output(
                ["docker", "inspect", container_id, "--format", "{{json .State}}"], text=True
            )
        )
        oom += int(bool(state.get("OOMKilled")))
        restart_count = int(
            subprocess.check_output(
                ["docker", "inspect", container_id, "--format", "{{.RestartCount}}"], text=True
            ).strip()
        )
        restarts += restart_count
    return validate_resources(
        {
            "runtime_host": platform.node(),
            "docker_mem_total_bytes": mem_total,
            "running_services": sorted(running_services),
            "aggregate_rss_mib": round(rss, 3),
            "oom_count": oom,
            "unexpected_restart_count": restarts,
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--grafana-base", default="http://127.0.0.1:3300")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    values = parse_dotenv(args.env_file)

    runs, started_at, ended_at = run_cohort(args.api_base)
    panels = query_panels(
        args.grafana_base,
        started_at,
        ended_at,
        expected_run_count=len(runs),
    )
    observability = query_cohort_observability(args.grafana_base, values, runs)
    tempo = observability["tempo"]
    langfuse = observability["langfuse"]
    resources = docker_resources(args.env_file)

    sentry_probe_path = LAB_ROOT / "artifacts" / "m2" / "sentry_probe.json"
    sentry_path = LAB_ROOT / "artifacts" / "m2" / "sentry_issue_verified.json"
    alert_path = LAB_ROOT / "artifacts" / "m2" / "alert_verified.json"
    sentry = validate_sentry_issue(sentry_probe_path, sentry_path)
    alert = json.loads(alert_path.read_text()) if alert_path.exists() else {"ok": False, "reason": "missing alert verification"}
    gates = {
        "G1_trace_complete": bool(tempo["ok"]),
        "G2_dashboard_metrics": bool(panels["ok"]),
        "G3_langfuse_complete": bool(langfuse["ok"]),
        "G4_sentry_issue": bool(sentry.get("ok")),
        "G5_alert_firing": bool(alert.get("ok")),
    }
    evidence = {
        "stage": "M2",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "cohort": {"started_at": started_at, "ended_at": ended_at, **validate_cohort(runs), "runs": runs},
        "panels": panels,
        "tempo": tempo,
        "langfuse": langfuse,
        "sentry": sentry,
        "alert": alert,
        "resources": resources,
        "gates": gates,
        "all_passed": all(gates.values()),
    }
    write_redacted_evidence(
        args.output,
        evidence,
        [values.get("SENTRY_DSN", ""), values.get("LANGFUSE_PUBLIC_KEY", ""), values.get("LANGFUSE_SECRET_KEY", "")],
    )
    print(json.dumps({"all_passed": evidence["all_passed"], "gates": gates}, ensure_ascii=False))
    return 0 if evidence["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
