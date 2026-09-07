#!/usr/bin/env python3
"""Run the sole isolated M3 chaos/load gate on the Compose runtime host."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from redis import Redis
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.agent.llm import OllamaClient  # noqa: E402
from app.agent.loop import SYSTEM_PROMPT  # noqa: E402
from app.agent.tools import TOOL_SCHEMAS  # noqa: E402
from app.models import AgentRun, BudgetAudit, OutboxJob, RunEvent, ToolCall  # noqa: E402


DEFAULT_OUTPUT = LAB_ROOT / "artifacts" / "m3" / "gate_m3.json"
DEFAULT_ENV = LAB_ROOT / "secrets" / ".env"
TERMINAL = {"completed", "failed", "cancelled"}
REDIS_RECOVERY_WORKERS = 4
EXACT_CAMPAIGN_INSTRUCTION = (
    "调用工具时必须原样保留标识符，campaign_id 必须是精确字符串 camp_001，"
    "不得省略 camp_ 前缀。"
)
EXPERIMENT_SIZES = {
    "worker_crashes": 10,
    "redis_outage_runs": 60,
    "provider_arm_runs": 30,
    "load_arm_runs": 500,
    "load_concurrency": 50,
    "sse_clients": 20,
    "sse_reconnects": 5,
}


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise AssertionError("percentile requires at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def parse_memory_to_mib(raw: str) -> float:
    value = raw.split("/", 1)[0].strip()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([kKMGT]i?B)", value)
    if match is None:
        raise ValueError(f"unsupported Docker memory value: {raw!r}")
    number, unit = match.groups()
    return float(number) * {
        "KiB": 1 / 1024,
        "MiB": 1,
        "GiB": 1024,
        "TiB": 1024 * 1024,
        "kB": 1 / 1000,
        "MB": 1,
        "GB": 1000,
        "TB": 1000 * 1000,
    }[unit]


def validate_tool_contract(calls: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [
        call
        for call in calls
        if int(call.get("call_count", 1)) == 1
        and call.get("tool_name") == "adjust_budget"
        and call.get("campaign_id") == "camp_001"
        and float(call.get("delta", math.nan)) == 1
    ]
    if len(calls) != 3 or len(valid) != 3:
        raise AssertionError("M3 tool contract preflight requires 3/3 exact calls")
    return {"ok": True, "sample_count": 3, "calls": calls}


def wait_for_value(
    read: Callable[[], Any],
    expected: Any,
    *,
    timeout_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Any:
    deadline = monotonic() + timeout_seconds
    value = read()
    while value != expected and monotonic() < deadline:
        sleep(0.2)
        value = read()
    return value


def summarize_run_timings(runs: list[dict[str, Any]]) -> dict[str, Any]:
    if not runs:
        raise AssertionError("run timing summary cannot be empty")
    durations = [
        (run["terminal_at"] - run["started_at"]).total_seconds()
        for run in runs
        if run.get("started_at") is not None and run.get("terminal_at") is not None
    ]
    queue_lags = [
        (run["started_at"] - run["created_at"]).total_seconds()
        for run in runs
        if run.get("started_at") is not None
    ]
    if len(durations) != len(runs) or len(queue_lags) != len(runs):
        raise AssertionError("every run needs created, started, and terminal timestamps")
    return {
        "count": len(runs),
        "completed": sum(run["status"] == "completed" for run in runs),
        "failed": sum(run["status"] == "failed" for run in runs),
        "run_p95_seconds": round(_percentile(durations, 0.95), 6),
        "queue_lag_p95_seconds": round(_percentile(queue_lags, 0.95), 6),
        "failed_rate": sum(run["status"] == "failed" for run in runs) / len(runs),
    }


def validate_worker_crashes(trials: list[dict[str, Any]]) -> dict[str, Any]:
    if len(trials) != EXPERIMENT_SIZES["worker_crashes"] or not all(
        trial.get("injected") for trial in trials
    ):
        raise AssertionError("worker crash gate requires exactly ten injected crashes")
    if sum(trial.get("status") == "completed" for trial in trials) < 9:
        raise AssertionError("fewer than nine worker crash runs completed")
    if any(int(trial.get("audit_rows", 0)) != 1 for trial in trials):
        raise AssertionError("duplicate or missing budget audit detected")
    if any(float(trial.get("recovery_seconds", 46)) > 45 for trial in trials):
        raise AssertionError("worker recovery exceeded 45 seconds")
    return {"ok": True, "trials": trials}


def validate_load_arms(one: dict[str, Any], four: dict[str, Any]) -> dict[str, Any]:
    expected = EXPERIMENT_SIZES["load_arm_runs"]
    if int(one.get("count", 0)) != expected or int(four.get("count", 0)) != expected:
        raise AssertionError("load arms must each contain exactly 500 runs")
    if float(one.get("post_p95_ms", math.inf)) >= 150 or float(
        four.get("post_p95_ms", math.inf)
    ) >= 150:
        raise AssertionError("load arm POST P95 exceeded 150 ms")
    if float(four.get("queue_lag_p95_seconds", math.inf)) > float(
        one.get("queue_lag_p95_seconds", 0)
    ) * 0.5:
        raise AssertionError("four workers did not reduce queue lag P95 by at least 50%")
    return {
        "ok": True,
        "one_worker": one,
        "four_workers": four,
        "queue_lag_reduction": 1
        - float(four["queue_lag_p95_seconds"]) / float(one["queue_lag_p95_seconds"]),
    }


def validate_provider_degradation(
    rate_limited: dict[str, Any], timed_out: dict[str, Any], recovered: bool
) -> bool:
    if rate_limited["completed"] / 30 < 0.70 or timed_out["completed"] / 30 < 0.70:
        raise AssertionError("provider degraded arm completion rate was below 70%")
    if rate_limited["retry_events"] <= 0 or timed_out["retry_events"] <= 0:
        raise AssertionError("provider degraded arm did not record retry events")
    if rate_limited["proxy"]["counts"]["429"] <= 0:
        raise AssertionError("429 arm did not inject a visible 429")
    if timed_out["proxy"]["counts"]["timeout"] <= 0:
        raise AssertionError("timeout arm did not inject a visible timeout")
    if any(
        count != 0
        for code, count in rate_limited["failure_codes"].items()
        if code != "MODEL_429"
    ):
        raise AssertionError("429 arm contained an unexpected failure code")
    if any(
        count != 0
        for code, count in timed_out["failure_codes"].items()
        if code != "MODEL_TIMEOUT"
    ):
        raise AssertionError("timeout arm contained an unexpected failure code")
    if not (rate_limited["alert_fired"] or timed_out["alert_fired"]):
        raise AssertionError("queue alert did not fire during either degraded arm")
    if not recovered:
        raise AssertionError("queue alert did not resolve after provider recovery")
    return True


def validate_sse_clients(clients: list[dict[str, Any]]) -> dict[str, Any]:
    if len(clients) != EXPERIMENT_SIZES["sse_clients"]:
        raise AssertionError("SSE gate requires exactly 20 clients")
    for client in clients:
        received = client.get("received", [])
        expected = client.get("database", [])
        if int(client.get("reconnects", 0)) != EXPERIMENT_SIZES["sse_reconnects"]:
            raise AssertionError("every SSE client must reconnect five times")
        if received != expected or len(received) != len(set(received)):
            raise AssertionError("SSE sequence is missing or duplicated")
    return {"ok": True, "clients": clients}


def validate_gate(
    experiments: dict[str, dict[str, Any]],
    *,
    duplicate_audit_keys: int,
    comparison: dict[str, dict[str, float]],
) -> dict[str, Any]:
    required_experiments = {f"EXP-{index}" for index in range(1, 7)}
    if set(experiments) != required_experiments or not all(
        bool(experiments[name].get("ok")) for name in required_experiments
    ):
        raise AssertionError("all six registered experiments must pass")
    if duplicate_audit_keys != 0:
        raise AssertionError("duplicate budget audit keys detected")
    required_metrics = {"run_p95_seconds", "queue_lag_p95_seconds", "failed_rate"}
    required_arms = {"normal", "429", "load_one", "load_four"}
    if set(comparison) != required_metrics:
        raise AssertionError("comparison table is missing metrics")
    for values in comparison.values():
        if set(values) != required_arms or not all(math.isfinite(float(value)) for value in values.values()):
            raise AssertionError("comparison table is incomplete")
    return {
        "all_passed": True,
        "gates": {
            "G1_six_experiments": True,
            "G2_zero_duplicate_side_effects": True,
            "G3_complete_baseline_table": True,
        },
    }


def write_redacted_evidence(path: Path, evidence: dict[str, Any], secrets: list[str]) -> None:
    serialized = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    if any(secret and secret in serialized for secret in secrets):
        raise AssertionError("secret value found in M3 evidence")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")


def parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _json_request(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> Any:
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


class M3Verifier:
    def __init__(
        self,
        *,
        api_base: str,
        grafana_base: str,
        proxy_base: str,
        database_url: str,
        normal_database_url: str,
        redis_url: str,
        env_file: Path,
        output: Path,
        secret_values: list[str],
    ) -> None:
        if not database_url.rsplit("/", 1)[-1].startswith("harness_m3"):
            raise AssertionError("M3 verifier refuses a non-harness_m3 database")
        redis_path = urllib.parse.urlparse(redis_url).path.strip("/")
        if not redis_path.isdigit() or int(redis_path) <= 0:
            raise AssertionError("M3 verifier requires an isolated non-zero Redis DB")
        self.api_base = api_base.rstrip("/")
        self.grafana_base = grafana_base.rstrip("/")
        self.proxy_base = proxy_base.rstrip("/")
        self.database_url = database_url
        self.normal_database_url = normal_database_url
        self.redis_url = redis_url
        self.env_file = env_file
        self.output = output
        self.secret_values = secret_values
        self.engine = create_engine(database_url, pool_pre_ping=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.experiments: dict[str, dict[str, Any]] = {}
        self.comparison: dict[str, dict[str, float]] = {
            metric: {} for metric in ("run_p95_seconds", "queue_lag_p95_seconds", "failed_rate")
        }
        self.compose_env = {
            "HARNESS_COMPOSE_DATABASE_URL": database_url,
            "HARNESS_COMPOSE_REDIS_URL": redis_url,
            "HARNESS_COMPOSE_LLM_BASE_URL": "http://chaos-proxy:9000/v1",
        }

    def close(self) -> None:
        self.engine.dispose()

    def _compose(
        self,
        arguments: list[str],
        *,
        overrides: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(self.compose_env)
        if overrides:
            environment.update(overrides)
        result = subprocess.run(
            ["docker", "compose", "--env-file", str(self.env_file), "--profile", "m3", *arguments],
            cwd=LAB_ROOT,
            env=environment,
            check=False,
            text=True,
            capture_output=True,
        )
        if check and result.returncode:
            diagnostic = (result.stderr or result.stdout).strip()[-4000:]
            raise RuntimeError(
                f"M3 Compose command failed ({result.returncode}): {arguments!r}: {diagnostic}"
            )
        return result

    def _wait_http(self, url: str, timeout_seconds: float = 120) -> Any:
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return _json_request(url, timeout=5)
            except Exception as error:
                last_error = error
                time.sleep(1)
        raise AssertionError(f"endpoint did not become ready: {url}: {last_error}")

    def configure_proxy(
        self,
        *,
        seed: int = 0,
        rate_429: float = 0,
        rate_timeout: float = 0,
        rate_5xx: float = 0,
        latency_ms: int = 0,
        timeout_seconds: float = 31,
    ) -> dict[str, Any]:
        overrides = {
            "CHAOS_SEED": str(seed),
            "CHAOS_429_RATE": str(rate_429),
            "CHAOS_TIMEOUT_RATE": str(rate_timeout),
            "CHAOS_5XX_RATE": str(rate_5xx),
            "CHAOS_LATENCY_MS": str(latency_ms),
            "CHAOS_TIMEOUT_SECONDS": str(timeout_seconds),
        }
        self._compose(["up", "-d", "--force-recreate", "chaos-proxy"], overrides=overrides)
        return self._wait_http(f"{self.proxy_base}/health")

    def scale_workers(self, count: int, *, pause_after_tool: int = 0) -> list[str]:
        self._compose(
            ["up", "-d", "--force-recreate", "--scale", f"worker={count}", "worker"],
            overrides={"HARNESS_TEST_PAUSE_AFTER_TOOL_SECONDS": str(pause_after_tool)},
        )
        deadline = time.monotonic() + 60
        ids: list[str] = []
        while time.monotonic() < deadline:
            result = self._compose(["ps", "-q", "worker"])
            ids = [line for line in result.stdout.splitlines() if line]
            if len(ids) == count:
                return ids
            time.sleep(1)
        raise AssertionError(f"expected {count} worker containers, got {ids}")

    def configure_dispatcher(self, *, crash_after_publish: bool = False) -> str:
        self._compose(
            ["up", "-d", "--force-recreate", "dispatcher"],
            overrides={
                "CHAOS_DISPATCHER_CRASH_AFTER_PUBLISH": str(crash_after_publish).lower()
            },
        )
        ids = [line for line in self._compose(["ps", "-q", "dispatcher"]).stdout.splitlines() if line]
        if len(ids) != 1:
            raise AssertionError(f"expected one dispatcher container, got {ids}")
        return ids[0]

    def create_run(self, prompt: str, key: str) -> tuple[str, float]:
        started = time.perf_counter()
        payload = _json_request(
            f"{self.api_base}/runs",
            method="POST",
            body={"prompt": prompt},
            headers={"Idempotency-Key": key},
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        return str(payload["run_id"]), elapsed_ms

    def create_batch(
        self,
        *,
        count: int,
        concurrency: int,
        prompt: str,
        prefix: str,
    ) -> tuple[list[str], list[float]]:
        def create(index: int) -> tuple[str, float]:
            return self.create_run(prompt, f"{prefix}-{index}")

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(create, range(count)))
        return [item[0] for item in results], [item[1] for item in results]

    def wait_runs(
        self,
        run_ids: list[str],
        *,
        timeout_seconds: float,
        observe_alert: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        deadline = time.monotonic() + timeout_seconds
        terminal: dict[str, dict[str, Any]] = {}
        alert_fired = False
        next_progress = time.monotonic() + 10
        while len(terminal) < len(run_ids) and time.monotonic() < deadline:
            for run_id in run_ids:
                if run_id in terminal:
                    continue
                view = _json_request(f"{self.api_base}/runs/{run_id}")
                if view["status"] in TERMINAL:
                    terminal[run_id] = view
            if observe_alert:
                alert_fired = alert_fired or self.queue_alert_active()
            if time.monotonic() >= next_progress:
                print(
                    json.dumps(
                        {"progress": len(terminal), "total": len(run_ids), "alert_fired": alert_fired}
                    ),
                    flush=True,
                )
                next_progress = time.monotonic() + 10
            if len(terminal) < len(run_ids):
                time.sleep(1)
        if len(terminal) != len(run_ids):
            missing = [run_id for run_id in run_ids if run_id not in terminal]
            raise AssertionError(f"runs did not reach terminal state: {missing[:5]}")
        return [terminal[run_id] for run_id in run_ids], alert_fired

    def run_records(self, run_ids: list[str]) -> list[dict[str, Any]]:
        with self.sessions() as session:
            runs = {
                run.id: run
                for run in session.scalars(select(AgentRun).where(AgentRun.id.in_(run_ids))).all()
            }
            events = session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id.in_(run_ids))
                .order_by(RunEvent.run_id, RunEvent.sequence)
            ).all()
        grouped: dict[str, list[RunEvent]] = {run_id: [] for run_id in run_ids}
        for event in events:
            grouped[event.run_id].append(event)
        records = []
        for run_id in run_ids:
            run = runs[run_id]
            started = next((event.created_at for event in grouped[run_id] if event.type == "run.started"), None)
            terminal = next(
                (
                    event.created_at
                    for event in grouped[run_id]
                    if event.type in {"run.completed", "run.failed", "run.cancelled"}
                ),
                None,
            )
            records.append(
                {
                    "run_id": run_id,
                    "status": run.status,
                    "error_code": run.error_code,
                    "created_at": run.created_at,
                    "started_at": started,
                    "terminal_at": terminal,
                    "events": [
                        {
                            "sequence": event.sequence,
                            "type": event.type,
                            "payload": event.payload,
                            "created_at": event.created_at,
                        }
                        for event in grouped[run_id]
                    ],
                }
            )
        return records

    def queue_alert_active(self) -> bool:
        token = base64.b64encode(b"admin:admin").decode("ascii")
        alerts = _json_request(
            f"{self.grafana_base}/api/alertmanager/grafana/api/v2/alerts",
            headers={"Authorization": f"Basic {token}"},
        )
        return any(
            alert.get("labels", {}).get("alertname") == "Harness queue lag"
            and alert.get("status", {}).get("state") == "active"
            for alert in alerts
        )

    def wait_alert_resolved(self, timeout_seconds: float = 180) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self.queue_alert_active():
                return True
            time.sleep(5)
        return False

    def pending_outbox(self) -> int:
        with self.sessions() as session:
            return int(
                session.scalar(
                    select(func.count()).select_from(OutboxJob).where(OutboxJob.status == "pending")
                )
                or 0
            )

    def ensure_quiescent(self) -> None:
        with self.sessions() as session:
            active = int(
                session.scalar(
                    select(func.count())
                    .select_from(AgentRun)
                    .where(AgentRun.status.in_(("queued", "running")))
                )
                or 0
            )
        queued = Redis.from_url(self.redis_url).llen("rq:queue:runs")
        if active or self.pending_outbox() or queued:
            raise AssertionError(
                f"M3 runtime is not quiescent: active={active}, outbox={self.pending_outbox()}, redis={queued}"
            )

    def save_experiment(self, name: str, evidence: dict[str, Any]) -> None:
        self.experiments[name] = evidence
        write_redacted_evidence(
            self.output.parent / f"{name.lower().replace('-', '_')}.json",
            evidence,
            self.secret_values,
        )

    def preflight_tool_contract(self) -> dict[str, Any]:
        self.configure_proxy()
        client = OllamaClient(base_url=f"{self.proxy_base}/v1")
        prompt = (
            "[M3-PREFLIGHT] 将 camp_001 的预算增加 1；必须调用 adjust_budget。"
            f"{EXACT_CAMPAIGN_INSTRUCTION}"
        )
        calls: list[dict[str, Any]] = []
        for _ in range(3):
            turn = client.complete(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                TOOL_SCHEMAS,
            )
            record: dict[str, Any] = {"call_count": len(turn.tool_calls)}
            if len(turn.tool_calls) == 1:
                call = turn.tool_calls[0]
                record["tool_name"] = call.name
                try:
                    arguments = json.loads(call.arguments)
                except json.JSONDecodeError:
                    arguments = {}
                if isinstance(arguments, dict):
                    record["campaign_id"] = arguments.get("campaign_id")
                    record["delta"] = arguments.get("delta")
            calls.append(record)
        try:
            evidence = validate_tool_contract(calls)
        except AssertionError:
            write_redacted_evidence(
                self.output.parent / "preflight_tool_contract.json",
                {"ok": False, "calls": calls},
                self.secret_values,
            )
            raise
        write_redacted_evidence(
            self.output.parent / "preflight_tool_contract.json",
            evidence,
            self.secret_values,
        )
        return evidence

    def resource_snapshot(self) -> dict[str, Any]:
        stats = subprocess.check_output(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}"], text=True
        )
        rows = [
            json.loads(line)
            for line in stats.splitlines()
            if line and json.loads(line).get("Name", "").startswith("harness-lab-")
        ]

        rss = round(sum(parse_memory_to_mib(row["MemUsage"]) for row in rows), 3)
        container_ids = self._compose(["ps", "-q"]).stdout.split()
        oom = 0
        for container_id in container_ids:
            state = json.loads(
                subprocess.check_output(
                    ["docker", "inspect", container_id, "--format", "{{json .State}}"],
                    text=True,
                )
            )
            oom += int(bool(state.get("OOMKilled")))
        if rss >= 4096 or oom:
            raise AssertionError(f"M3 resource stop line reached: rss_mib={rss}, oom={oom}")
        return {"ok": True, "aggregate_rss_mib": rss, "oom_count": oom}

    def _record_comparison(self, arm: str, summary: dict[str, Any]) -> None:
        for metric in self.comparison:
            self.comparison[metric][arm] = float(summary[metric])

    def _run_id_digest(self, run_ids: list[str]) -> str:
        return hashlib.sha256("\n".join(run_ids).encode("utf-8")).hexdigest()

    def _audit_rows(self, run_id: str) -> int:
        with self.sessions() as session:
            return int(
                session.scalar(
                    select(func.count()).select_from(BudgetAudit).where(BudgetAudit.run_id == run_id)
                )
                or 0
            )

    def _wait_for_committed_tool(self, run_id: str, timeout_seconds: float = 90) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            with self.sessions() as session:
                succeeded = int(
                    session.scalar(
                        select(func.count())
                        .select_from(ToolCall)
                        .where(ToolCall.run_id == run_id, ToolCall.status == "succeeded")
                    )
                    or 0
                )
                status = session.scalar(select(AgentRun.status).where(AgentRun.id == run_id))
            if succeeded:
                return True
            if status in TERMINAL:
                return False
            time.sleep(0.2)
        return False

    def experiment_worker_crash(self) -> dict[str, Any]:
        self.ensure_quiescent()
        self.configure_proxy()
        trials: list[dict[str, Any]] = []
        skipped: list[str] = []
        candidates = 0
        while len(trials) < EXPERIMENT_SIZES["worker_crashes"] and candidates < 15:
            candidates += 1
            worker_ids = self.scale_workers(1, pause_after_tool=120)
            run_id, _ = self.create_run(
                f"[M3-EXP1-{candidates}] 将 camp_001 的预算增加 1；必须调用 adjust_budget，"
                f"完成后汇报新预算。{EXACT_CAMPAIGN_INSTRUCTION}",
                f"m3-exp1-{candidates}-{time.time_ns()}",
            )
            if not self._wait_for_committed_tool(run_id):
                skipped.append(run_id)
                continue
            killed_at = datetime.now(timezone.utc)
            subprocess.run(["docker", "kill", worker_ids[0]], check=True, capture_output=True, text=True)
            self.scale_workers(1, pause_after_tool=0)
            views, _ = self.wait_runs([run_id], timeout_seconds=120)
            record = self.run_records([run_id])[0]
            starts = [event for event in record["events"] if event["type"] == "run.started"]
            recovery_start = next(
                (
                    event["created_at"]
                    for event in starts
                    if int(event["payload"].get("attempt", 0)) >= 2
                ),
                None,
            )
            recovery_seconds = (
                (recovery_start - killed_at).total_seconds() if recovery_start is not None else math.inf
            )
            trials.append(
                {
                    "run_id": run_id,
                    "injected": True,
                    "status": views[0]["status"],
                    "attempt": views[0]["attempt"],
                    "recovery_seconds": round(recovery_seconds, 3),
                    "audit_rows": self._audit_rows(run_id),
                    "started_events": len(starts),
                }
            )
        try:
            result = validate_worker_crashes(trials)
        except AssertionError:
            self.save_experiment("EXP-1", {"ok": False, "trials": trials, "skipped": skipped})
            raise
        result["skipped_non_tool_runs"] = skipped
        self.save_experiment("EXP-1", result)
        return result

    def experiment_dispatcher_crash(self) -> dict[str, Any]:
        self.ensure_quiescent()
        marker = LAB_ROOT / "data" / "chaos" / "dispatcher-after-publish.once"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.unlink(missing_ok=True)
        dispatcher_id = self.configure_dispatcher(crash_after_publish=True)
        subprocess.run(
            ["docker", "update", "--restart=no", dispatcher_id],
            check=True,
            capture_output=True,
            text=True,
        )
        run_id, _ = self.create_run(
            "[M3-EXP2] 只回复 OK，不调用工具",
            f"m3-exp2-{time.time_ns()}",
        )
        deadline = time.monotonic() + 30
        exit_code = None
        while time.monotonic() < deadline:
            state = json.loads(
                subprocess.check_output(
                    ["docker", "inspect", dispatcher_id, "--format", "{{json .State}}"], text=True
                )
            )
            if marker.exists() and not state.get("Running"):
                exit_code = int(state.get("ExitCode", -1))
                break
            time.sleep(0.2)
        with self.sessions() as session:
            outbox = session.scalar(
                select(OutboxJob).where(OutboxJob.payload["run_id"].astext == run_id)
            )
            pending_seen = outbox is not None and outbox.status == "pending"
        self.configure_dispatcher(crash_after_publish=False)
        views, _ = self.wait_runs([run_id], timeout_seconds=120)
        record = self.run_records([run_id])[0]
        started_events = sum(event["type"] == "run.started" for event in record["events"])
        terminal_events = sum(
            event["type"] in {"run.completed", "run.failed"} for event in record["events"]
        )
        def read_outbox_status() -> str | None:
            with self.sessions() as session:
                return session.scalar(
                    select(OutboxJob.status).where(OutboxJob.payload["run_id"].astext == run_id)
                )

        outbox_status = wait_for_value(
            read_outbox_status,
            "dispatched",
            timeout_seconds=10,
        )
        evidence = {
            "ok": bool(
                exit_code == 91
                and pending_seen
                and views[0]["status"] in TERMINAL
                and started_events == 1
                and terminal_events == 1
                and outbox_status == "dispatched"
            ),
            "run_id": run_id,
            "dispatcher_exit_code": exit_code,
            "pending_after_crash": pending_seen,
            "final_status": views[0]["status"],
            "started_events": started_events,
            "terminal_events": terminal_events,
            "outbox_status": outbox_status,
        }
        self.save_experiment("EXP-2", evidence)
        if not evidence["ok"]:
            raise AssertionError("dispatcher crash experiment failed registered invariants")
        return evidence

    def experiment_redis_outage(self) -> dict[str, Any]:
        self.ensure_quiescent()
        self.configure_proxy()
        self.scale_workers(1)
        self.configure_dispatcher(crash_after_publish=False)
        self._compose(["stop", "redis"])
        run_ids: list[str] = []
        post_ms: list[float] = []
        started = time.monotonic()
        try:
            for index in range(EXPERIMENT_SIZES["redis_outage_runs"]):
                target = started + index
                if time.monotonic() < target:
                    time.sleep(target - time.monotonic())
                run_id, elapsed = self.create_run(
                    f"[M3-EXP3-{index}] 只回复 OK，不调用工具",
                    f"m3-exp3-{index}-{time.time_ns()}",
                )
                run_ids.append(run_id)
                post_ms.append(elapsed)
            with self.sessions() as session:
                queued_during = int(
                    session.scalar(
                        select(func.count())
                        .select_from(AgentRun)
                        .where(AgentRun.id.in_(run_ids), AgentRun.status == "queued")
                    )
                    or 0
                )
            pending_during = self.pending_outbox()
        finally:
            self._compose(["start", "redis"])
            deadline = time.monotonic() + 30
            redis_client = Redis.from_url(self.redis_url)
            while time.monotonic() < deadline:
                try:
                    if redis_client.ping():
                        break
                except Exception:
                    pass
                time.sleep(1)
            else:
                raise AssertionError("Redis did not recover")
            self.configure_dispatcher(crash_after_publish=False)
            self.scale_workers(REDIS_RECOVERY_WORKERS)
        recovery_started = time.monotonic()
        views, _ = self.wait_runs(run_ids, timeout_seconds=120)
        drain_seconds = time.monotonic() - recovery_started
        evidence = {
            "ok": bool(
                len(run_ids) == 60
                and queued_during == 60
                and pending_during == 60
                and len(views) == 60
                and self.pending_outbox() == 0
                and drain_seconds <= 120
            ),
            "created": len(run_ids),
            "post_success": len(run_ids),
            "post_p95_ms": round(_percentile(post_ms, 0.95), 3),
            "queued_while_down": queued_during,
            "pending_outbox_while_down": pending_during,
            "recovery_workers": REDIS_RECOVERY_WORKERS,
            "terminal_after_recovery": len(views),
            "drain_seconds": round(drain_seconds, 3),
            "run_ids_sha256": self._run_id_digest(run_ids),
        }
        self.save_experiment("EXP-3", evidence)
        if not evidence["ok"]:
            raise AssertionError("Redis outage experiment failed registered invariants")
        return evidence

    def _provider_arm(
        self,
        name: str,
        *,
        seed: int,
        rate_429: float = 0,
        rate_timeout: float = 0,
        timeout_seconds: float = 31,
        timeout: float = 1800,
    ) -> dict[str, Any]:
        self.ensure_quiescent()
        self.configure_proxy(
            seed=seed,
            rate_429=rate_429,
            rate_timeout=rate_timeout,
            timeout_seconds=timeout_seconds,
        )
        self.scale_workers(1)
        prompt = (
            f"[M3-{name}] 诊断 camp_001 今日消耗并汇报预算使用率；"
            f"必须先调用 get_report。{EXACT_CAMPAIGN_INSTRUCTION}"
        )
        run_ids, post_ms = self.create_batch(
            count=EXPERIMENT_SIZES["provider_arm_runs"],
            concurrency=30,
            prompt=prompt,
            prefix=f"m3-{name.lower()}-{time.time_ns()}",
        )
        views, alert_fired = self.wait_runs(
            run_ids, timeout_seconds=timeout, observe_alert=(rate_429 > 0 or rate_timeout > 0)
        )
        records = self.run_records(run_ids)
        summary = summarize_run_timings(records)
        retry_events = [
            event
            for record in records
            for event in record["events"]
            if event["type"] == "step.model_retry"
        ]
        failures = {
            code: sum(view.get("error_code") == code for view in views)
            for code in ("MODEL_429", "MODEL_TIMEOUT", "MODEL_5XX", "BAD_OUTPUT", "INTERNAL_ERROR")
        }
        proxy = _json_request(f"{self.proxy_base}/health")
        return {
            **summary,
            "post_p95_ms": round(_percentile(post_ms, 0.95), 3),
            "retry_events": len(retry_events),
            "failure_codes": failures,
            "alert_fired": alert_fired,
            "proxy": proxy,
            "run_ids_sha256": self._run_id_digest(run_ids),
        }

    def experiment_provider_degradation(self) -> dict[str, Any]:
        normal = self._provider_arm("NORMAL", seed=11, timeout=900)
        self._record_comparison("normal", normal)
        if not self.wait_alert_resolved():
            raise AssertionError("queue alert did not resolve after normal arm")
        rate_limited = self._provider_arm("429", seed=0, rate_429=0.5, timeout=1200)
        self._record_comparison("429", rate_limited)
        if not self.wait_alert_resolved():
            raise AssertionError("queue alert did not resolve after 429 arm")
        timed_out = self._provider_arm(
            "TIMEOUT", seed=7, rate_timeout=0.3, timeout_seconds=31, timeout=2400
        )
        self.configure_proxy()
        recovered = self.wait_alert_resolved()
        evidence = {
            "ok": False,
            "normal": normal,
            "rate_429": rate_limited,
            "timeout": timed_out,
            "alert_recovered": recovered,
        }
        try:
            validate_provider_degradation(rate_limited, timed_out, recovered)
        except AssertionError:
            self.save_experiment("EXP-4", evidence)
            raise
        evidence["ok"] = True
        self.save_experiment("EXP-4", evidence)
        return evidence

    def _load_arm(self, workers: int, name: str) -> dict[str, Any]:
        self.ensure_quiescent()
        self.configure_proxy(seed=19, latency_ms=300)
        self.scale_workers(workers)
        run_ids, post_ms = self.create_batch(
            count=EXPERIMENT_SIZES["load_arm_runs"],
            concurrency=EXPERIMENT_SIZES["load_concurrency"],
            prompt=f"[M3-{name}] 只回复 OK，不调用工具",
            prefix=f"m3-{name.lower()}-{time.time_ns()}",
        )
        views, _ = self.wait_runs(run_ids, timeout_seconds=2400)
        records = self.run_records(run_ids)
        summary = summarize_run_timings(records)
        first_started = min(record["started_at"] for record in records)
        last_terminal = max(record["terminal_at"] for record in records)
        elapsed = (last_terminal - first_started).total_seconds()
        return {
            **summary,
            "post_p95_ms": round(_percentile(post_ms, 0.95), 3),
            "throughput_runs_per_minute": round(summary["completed"] / elapsed * 60, 3),
            "run_ids_sha256": self._run_id_digest(run_ids),
            "terminal": len(views),
        }

    def experiment_load(self) -> dict[str, Any]:
        one = self._load_arm(1, "LOAD-ONE")
        self._record_comparison("load_one", one)
        four = self._load_arm(4, "LOAD-FOUR")
        self._record_comparison("load_four", four)
        try:
            evidence = validate_load_arms(one, four)
        except AssertionError:
            self.save_experiment("EXP-5", {"ok": False, "one_worker": one, "four_workers": four})
            raise
        self.save_experiment("EXP-5", evidence)
        return evidence

    def _read_sse(self, run_id: str, last_event_id: int, max_events: int | None) -> list[int]:
        headers = {"Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = str(last_event_id)
        request = urllib.request.Request(f"{self.api_base}/runs/{run_id}/events", headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        received: list[int] = []
        current_id: int | None = None
        with opener.open(request, timeout=120) as response:
            for raw in response:
                line = raw.decode("utf-8").strip()
                if line.startswith("id:"):
                    current_id = int(line.split(":", 1)[1].strip())
                elif not line and current_id is not None:
                    received.append(current_id)
                    current_id = None
                    if max_events is not None and len(received) >= max_events:
                        break
        return received

    def _sse_client(self, run_id: str) -> dict[str, Any]:
        received: list[int] = []
        cursor = 0
        for _ in range(EXPERIMENT_SIZES["sse_reconnects"]):
            batch = self._read_sse(run_id, cursor, 1)
            if batch:
                received.extend(batch)
                cursor = batch[-1]
        tail = self._read_sse(run_id, cursor, None)
        received.extend(tail)
        with self.sessions() as session:
            database = session.scalars(
                select(RunEvent.sequence)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.sequence)
            ).all()
        return {
            "run_id": run_id,
            "reconnects": 5,
            "received": received,
            "database": list(database),
        }

    def experiment_sse(self) -> dict[str, Any]:
        self.ensure_quiescent()
        self.configure_proxy()
        self.scale_workers(4)
        run_ids, _ = self.create_batch(
            count=EXPERIMENT_SIZES["sse_clients"],
            concurrency=20,
            prompt="[M3-EXP6] 只回复 OK，不调用工具",
            prefix=f"m3-exp6-{time.time_ns()}",
        )
        with ThreadPoolExecutor(max_workers=20) as pool:
            clients = list(pool.map(self._sse_client, run_ids))
        self.wait_runs(run_ids, timeout_seconds=300)
        try:
            evidence = validate_sse_clients(clients)
        except AssertionError:
            self.save_experiment("EXP-6", {"ok": False, "clients": clients})
            raise
        self.save_experiment("EXP-6", evidence)
        return evidence

    def duplicate_audit_keys(self) -> int:
        with self.sessions() as session:
            duplicates = session.execute(
                select(BudgetAudit.tool_call_key, func.count())
                .group_by(BudgetAudit.tool_call_key)
                .having(func.count() > 1)
            ).all()
        return len(duplicates)

    def normal_database_m3_rows(self) -> int:
        engine = create_engine(self.normal_database_url, pool_pre_ping=True)
        try:
            with Session(engine) as session:
                return int(
                    session.scalar(
                        select(func.count())
                        .select_from(AgentRun)
                        .where(AgentRun.input_json["prompt"].astext.like("[M3-%"))
                    )
                    or 0
                )
        finally:
            engine.dispose()

    def run(self) -> dict[str, Any]:
        with self.sessions() as session:
            existing = int(session.scalar(select(func.count()).select_from(AgentRun)) or 0)
        if existing:
            raise AssertionError(
                f"harness_m3 is not empty ({existing} runs); refusing to rerun the sole Gate"
            )
        self._wait_http(f"{self.api_base}/health")
        self._wait_http(f"{self.proxy_base}/health")
        if self.normal_database_m3_rows() != 0:
            raise AssertionError("normal harness database already contains M3-tagged runs")
        preflight = self.preflight_tool_contract()
        resources_before = self.resource_snapshot()
        self.experiment_worker_crash()
        self.experiment_dispatcher_crash()
        self.experiment_redis_outage()
        self.experiment_provider_degradation()
        self.experiment_load()
        self.experiment_sse()
        self.configure_proxy()
        self.scale_workers(1)
        self.configure_dispatcher(crash_after_publish=False)
        self.ensure_quiescent()
        duplicate_keys = self.duplicate_audit_keys()
        normal_database_rows = self.normal_database_m3_rows()
        if normal_database_rows:
            raise AssertionError("M3 writes leaked into the normal harness database")
        gate = validate_gate(
            self.experiments,
            duplicate_audit_keys=duplicate_keys,
            comparison=self.comparison,
        )
        evidence = {
            "stage": "M3",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            **gate,
            "experiments": self.experiments,
            "duplicate_audit_keys": duplicate_keys,
            "comparison": self.comparison,
            "normal_database_m3_rows": normal_database_rows,
            "tool_contract_preflight": preflight,
            "resources_before": resources_before,
            "resources_after": self.resource_snapshot(),
        }
        write_redacted_evidence(self.output, evidence, self.secret_values)
        return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default="http://api:8000")
    parser.add_argument("--grafana-base", default="http://lgtm:3000")
    parser.add_argument("--proxy-base", default="http://chaos-proxy:9000")
    parser.add_argument(
        "--database-url",
        default="postgresql+psycopg://postgres:harness@postgres:5432/harness_m3",
    )
    parser.add_argument(
        "--normal-database-url",
        default="postgresql+psycopg://postgres:harness@postgres:5432/harness",
    )
    parser.add_argument("--redis-url", default="redis://redis:6379/1")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    values = parse_dotenv(args.env_file)
    secrets = [
        values.get("SENTRY_DSN", ""),
        values.get("LANGFUSE_PUBLIC_KEY", ""),
        values.get("LANGFUSE_SECRET_KEY", ""),
    ]
    verifier = M3Verifier(
        api_base=args.api_base,
        grafana_base=args.grafana_base,
        proxy_base=args.proxy_base,
        database_url=args.database_url,
        normal_database_url=args.normal_database_url,
        redis_url=args.redis_url,
        env_file=args.env_file,
        output=args.output,
        secret_values=secrets,
    )
    try:
        evidence = verifier.run()
    except Exception as error:
        write_redacted_evidence(
            args.output.parent / "failure.json",
            {
                "stage": "M3",
                "ok": False,
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error_type": type(error).__name__,
                "completed_experiments": sorted(verifier.experiments),
            },
            secrets,
        )
        raise
    finally:
        verifier.close()
    print(json.dumps({"all_passed": evidence["all_passed"], "gates": evidence["gates"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
