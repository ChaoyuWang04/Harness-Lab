#!/usr/bin/env python3
from __future__ import annotations

import math
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker


LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from app.config import settings  # noqa: E402
from app.models import (  # noqa: E402
    AgentRun,
    BudgetAudit,
    Campaign,
    IdempotencyKey,
    OutboxJob,
    RunEvent,
    ToolCall,
)


REQUIRED_LIFECYCLE = (
    "run.created",
    "run.enqueued",
    "run.started",
    "step.tool_call",
    "step.tool_result",
    "run.completed",
)
FOUR_GIB = 4 * 1024**3


def ordered_lifecycle_ok(event_types: list[str]) -> bool:
    cursor = 0
    for required in REQUIRED_LIFECYCLE:
        try:
            cursor = event_types.index(required, cursor) + 1
        except ValueError:
            return False
    return True


def _conservative_percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    index = math.ceil(quantile * (len(ordered) - 1))
    return float(ordered[index])


def evaluate_baseline(records: list[dict[str, Any]]) -> dict[str, Any]:
    completed = sum(record["status"] == "completed" for record in records)
    failures = [record.get("error_code") for record in records if record["status"] != "completed"]
    bad_outputs = failures.count("BAD_OUTPUT")
    systemic_failures = [code for code in failures if code != "BAD_OUTPUT"]
    post_p95_ms = _conservative_percentile([float(record["post_ms"]) for record in records], 0.95)
    lifecycle_failures = sum(
        record["status"] == "completed" and not ordered_lifecycle_ok(record["events"])
        for record in records
    )
    passed = (
        len(records) == 20
        and completed >= 18
        and bad_outputs <= 2
        and not systemic_failures
        and post_p95_ms < 100
        and lifecycle_failures == 0
    )
    return {
        "passed": passed,
        "total": len(records),
        "completed": completed,
        "bad_outputs": bad_outputs,
        "systemic_failures": systemic_failures,
        "post_p95_ms": post_p95_ms,
        "lifecycle_failures": lifecycle_failures,
    }


def sse_replay_ok(
    full_sequences: list[int],
    *,
    last_event_id: int,
    replayed: list[int],
) -> bool:
    expected = [sequence for sequence in full_sequences if sequence > last_event_id]
    return replayed == expected and len(replayed) == len(set(replayed))


def resource_gate_ok(containers: list[dict[str, Any]]) -> bool:
    return (
        sum(int(container["memory_bytes"]) for container in containers) <= FOUR_GIB
        and all(not container["oom_killed"] for container in containers)
        and all(int(container["restart_count"]) == 0 for container in containers)
    )


def _json_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], float]:
    request_headers = {"Accept": "application/json", **(headers or {})}
    body = None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:8000{path}",
        data=body,
        headers=request_headers,
        method=method,
    )
    started = time.perf_counter()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        result = json.loads(response.read())
    return result, (time.perf_counter() - started) * 1000


def _create_run(prompt: str, idempotency_key: str | None = None) -> tuple[str, float]:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    result, elapsed_ms = _json_request(
        "POST",
        "/runs",
        payload={"prompt": prompt},
        headers=headers,
    )
    return str(result["run_id"]), elapsed_ms


def _wait_terminal(run_ids: list[str], timeout_seconds: float) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    terminal: dict[str, dict[str, Any]] = {}
    last_reported = -1
    while time.monotonic() < deadline:
        for run_id in run_ids:
            if run_id in terminal:
                continue
            state, _ = _json_request("GET", f"/runs/{run_id}")
            if state["status"] in {"completed", "failed", "cancelled"}:
                terminal[run_id] = state
        if len(terminal) != last_reported:
            print(f"terminal progress: {len(terminal)}/{len(run_ids)}", flush=True)
            last_reported = len(terminal)
        if len(terminal) == len(run_ids):
            return terminal
        time.sleep(0.5)
    return terminal


def _clear_runtime(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        session.execute(delete(BudgetAudit))
        session.execute(delete(ToolCall))
        session.execute(delete(IdempotencyKey))
        session.execute(delete(OutboxJob))
        session.execute(delete(RunEvent))
        session.execute(delete(AgentRun))
        session.get(Campaign, "camp_001").budget = 1000


def _records(
    session_factory: sessionmaker[Session],
    run_ids: list[str],
    post_times: dict[str, float],
) -> list[dict[str, Any]]:
    records = []
    with session_factory() as session:
        for run_id in run_ids:
            run = session.get(AgentRun, run_id)
            event_types = session.scalars(
                select(RunEvent.type).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence)
            ).all()
            records.append(
                {
                    "run_id": run_id,
                    "status": run.status,
                    "error_code": run.error_code,
                    "post_ms": round(post_times[run_id], 3),
                    "events": event_types,
                }
            )
    return records


def _sse_replay(run_id: str, last_event_id: int) -> list[int]:
    request = urllib.request.Request(
        f"http://127.0.0.1:8000/runs/{run_id}/events",
        headers={"Last-Event-ID": str(last_event_id), "Accept": "text/event-stream"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        text = response.read().decode("utf-8")
    return [int(line[4:].strip()) for line in text.splitlines() if line.startswith("id:")]


def _compose(*args: str, extra_env: dict[str, str] | None = None) -> None:
    environment = os.environ.copy()
    if extra_env:
        environment.update(extra_env)
    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(LAB_ROOT / "secrets" / ".env"),
            "-f",
            str(LAB_ROOT / "compose.yaml"),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )


def _memory_bytes(value: str) -> int:
    number, unit = value.strip().split() if " " in value.strip() else (value[:-3], value[-3:])
    multipliers = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "kB": 1000, "MB": 1000**2, "GB": 1000**3}
    return round(float(number) * multipliers[unit])


def _resource_snapshot() -> list[dict[str, Any]]:
    stats = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    containers = []
    for line in stats.stdout.splitlines():
        item = json.loads(line)
        name = item.get("Name", "")
        if not name.startswith("harness-lab-"):
            continue
        usage = item["MemUsage"].split("/")[0].strip()
        state = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", name],
            check=True,
            capture_output=True,
            text=True,
        )
        state_data = json.loads(state.stdout)
        containers.append(
            {
                "name": name,
                "memory_bytes": _memory_bytes(usage),
                "oom_killed": bool(state_data["OOMKilled"]),
                "restart_count": int(
                    subprocess.run(
                        ["docker", "inspect", "--format", "{{.RestartCount}}", name],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()
                ),
            }
        )
    return containers


def run_live_gate() -> dict[str, Any]:
    engine = create_engine(settings.test_database_url, pool_pre_ping=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    evidence: dict[str, Any] = {"checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        _clear_runtime(sessions)
        resource = _resource_snapshot()
        evidence["resources"] = {
            "passed": resource_gate_ok(resource),
            "aggregate_memory_bytes": sum(item["memory_bytes"] for item in resource),
            "containers": resource,
        }

        prompt = "诊断 camp_001 今日消耗并汇报预算使用率；必须先调用工具取得数据"
        post_times: dict[str, float] = {}
        run_ids = []
        for index in range(20):
            run_id, elapsed = _create_run(prompt, f"m1-baseline-{index:02d}")
            run_ids.append(run_id)
            post_times[run_id] = elapsed
        terminal = _wait_terminal(run_ids, 180)
        print(f"baseline terminal: {len(terminal)}/20", flush=True)
        records = _records(sessions, run_ids, post_times)
        evidence["baseline"] = {**evaluate_baseline(records), "runs": records}

        completed_id = next((run_id for run_id in run_ids if terminal.get(run_id, {}).get("status") == "completed"), None)
        if completed_id:
            with sessions() as session:
                full_sequences = session.scalars(
                    select(RunEvent.sequence).where(RunEvent.run_id == completed_id).order_by(RunEvent.sequence)
                ).all()
            replayed = _sse_replay(completed_id, 2)
            evidence["sse"] = {
                "passed": sse_replay_ok(full_sequences, last_event_id=2, replayed=replayed),
                "last_event_id": 2,
                "full_sequences": full_sequences,
                "replayed": replayed,
            }
        else:
            evidence["sse"] = {"passed": False, "reason": "no completed run"}

        idem_ids = [_create_run("诊断 camp_001", "m1-idempotency-gate")[0] for _ in range(3)]
        with sessions() as session:
            idem_count = session.scalar(
                select(func.count()).select_from(AgentRun).where(AgentRun.id == idem_ids[0])
            )
        evidence["request_idempotency"] = {
            "passed": len(set(idem_ids)) == 1 and idem_count == 1,
            "unique_run_ids": len(set(idem_ids)),
            "database_rows": idem_count,
        }
        _wait_terminal([idem_ids[0]], 30)

        _compose(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "worker",
            extra_env={"HARNESS_TEST_PAUSE_AFTER_TOOL_SECONDS": "45"},
        )
        with sessions.begin() as session:
            session.get(Campaign, "camp_001").budget = 1000
        crash_run, _ = _create_run(
            "将 camp_001 的预算增加 100；必须调用 adjust_budget，完成后汇报新预算",
            "m1-worker-kill-gate",
        )
        audit_deadline = time.monotonic() + 60
        audit_seen = False
        while time.monotonic() < audit_deadline:
            with sessions() as session:
                audit_seen = bool(
                    session.scalar(
                        select(func.count()).select_from(BudgetAudit).where(BudgetAudit.run_id == crash_run)
                    )
                )
            if audit_seen:
                break
            time.sleep(0.25)
        if audit_seen:
            subprocess.run(["docker", "kill", "harness-lab-worker-1"], check=True, capture_output=True, text=True)
            killed_at = time.monotonic()
            _compose(
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "worker",
                extra_env={"HARNESS_TEST_PAUSE_AFTER_TOOL_SECONDS": "45"},
            )
            crash_terminal = _wait_terminal([crash_run], 60).get(crash_run)
        else:
            crash_terminal = None
            killed_at = None
        with sessions() as session:
            audit_count = session.scalar(
                select(func.count()).select_from(BudgetAudit).where(BudgetAudit.run_id == crash_run)
            )
            terminal_count = session.scalar(
                select(func.count()).select_from(RunEvent).where(
                    RunEvent.run_id == crash_run,
                    RunEvent.type.in_(("run.completed", "run.failed")),
                )
            )
            budget = float(session.get(Campaign, "camp_001").budget)
        evidence["worker_kill"] = {
            "passed": bool(
                audit_seen
                and crash_terminal
                and crash_terminal["status"] == "completed"
                and audit_count == 1
                and terminal_count == 1
                and budget == 1100
            ),
            "audit_seen_before_kill": audit_seen,
            "final_status": crash_terminal["status"] if crash_terminal else None,
            "audit_rows": audit_count,
            "terminal_events": terminal_count,
            "final_budget": budget,
            "run_id": crash_run,
            "recovery_seconds": (
                round(time.monotonic() - killed_at, 3)
                if killed_at is not None and crash_terminal is not None
                else None
            ),
        }

        _compose(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "worker",
            extra_env={"HARNESS_TEST_PAUSE_AFTER_TOOL_SECONDS": "0"},
        )
        _compose("stop", "redis")
        outage_ids = [_create_run(prompt, f"m1-redis-outage-{index}")[0] for index in range(3)]
        with sessions() as session:
            queued_while_down = session.scalar(
                select(func.count()).select_from(AgentRun).where(
                    AgentRun.id.in_(outage_ids), AgentRun.status == "queued"
                )
            )
        _compose("start", "redis")
        dispatch_deadline = time.monotonic() + 30
        dispatched = 0
        while time.monotonic() < dispatch_deadline:
            with sessions() as session:
                dispatched = session.scalar(
                    select(func.count()).select_from(OutboxJob).where(
                        OutboxJob.payload["run_id"].astext.in_(outage_ids),
                        OutboxJob.status == "dispatched",
                    )
                )
            if dispatched == len(outage_ids):
                break
            time.sleep(0.25)
        evidence["redis_outage"] = {
            "passed": queued_while_down == 3 and dispatched == 3,
            "post_succeeded_while_down": queued_while_down,
            "dispatched_within_30s": dispatched,
        }
    finally:
        try:
            _compose("start", "redis")
            _compose(
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "worker",
                extra_env={"HARNESS_TEST_PAUSE_AFTER_TOOL_SECONDS": "0"},
            )
        finally:
            engine.dispose()

    required = ("resources", "baseline", "sse", "request_idempotency", "worker_kill", "redis_outage")
    evidence["passed"] = all(evidence.get(name, {}).get("passed") for name in required)
    return evidence


def main() -> int:
    result = run_live_gate()
    output = LAB_ROOT / "artifacts" / "m1" / "gate_m1.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {name: result[name]["passed"] for name in ("resources", "baseline", "sse", "request_idempotency", "worker_kill", "redis_outage")}
    print(json.dumps({"passed": result["passed"], "checks": summary, "evidence": str(output)}, ensure_ascii=False), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
