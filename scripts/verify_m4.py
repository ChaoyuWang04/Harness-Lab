from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

import requests
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db import make_engine
from app.eval.artifacts import atomic_write_json, atomic_write_jsonl, canonical_json_bytes, sha256_file
from app.eval.catalog import EvalCase, canonical_json, load_eval_catalog, sha256_bytes
from app.eval.dataset import build_dataset, create_review_material, pseudonymized_fixture_state
from app.eval.export import export_trajectories
from app.eval.redact import normalize_trajectories
from app.eval.replay import run_live_replay, score_dataset
from app.models import AgentRun, BudgetAudit, Campaign, ModelTurnRecord, OutboxJob, RunEvent, ToolCall
from scripts.export_traces import extract_database_fixtures
from scripts.run_m4_cohort import CohortError, run_cohort, validate_gate_id


def aggregate_gate(criteria: list[dict[str, Any]], *, output: Path) -> dict[str, Any]:
    statuses = {item["status"] for item in criteria}
    if "FAIL" in statuses:
        status = "FAIL"
    elif "PENDING" in statuses:
        status = "PENDING_HUMAN"
    elif statuses <= {"PASS", "WARN"}:
        status = "PASS"
    else:
        status = "FAIL"
    result = {
        "schema_version": "m4_gate_v1",
        "status": status,
        "all_passed": status == "PASS",
        "criteria": criteria,
    }
    atomic_write_json(output, result, overwrite=output.exists())
    return result


class LiveCohortRuntime:
    def __init__(
        self,
        *,
        database_url: str,
        normal_database_url: str,
        redis_url: str,
        normal_redis_url: str,
        api_base: str,
        proxy_base: str,
        control_token: str,
        catalog,
    ) -> None:
        self.engine = make_engine(database_url)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.normal_engine = make_engine(normal_database_url)
        self.redis = Redis.from_url(redis_url)
        self.normal_redis = Redis.from_url(normal_redis_url)
        self.api_base = api_base.rstrip("/")
        self.proxy_base = proxy_base.rstrip("/")
        self.headers = {"x-harness-m4-token": control_token}
        self.catalog = catalog
        with self.normal_engine.connect() as connection:
            self.normal_run_count = int(connection.scalar(select(func.count()).select_from(AgentRun)) or 0)
        self.normal_redis_size = int(self.normal_redis.dbsize())

    def close(self) -> None:
        self.engine.dispose()
        self.normal_engine.dispose()

    def restore_fixture(self, case: EvalCase) -> str:
        fixture = self.catalog.world_fixtures[case.world_fixture_id]
        with self.sessions.begin() as session:
            for expected in fixture.campaigns:
                campaign = session.get(Campaign, expected.id)
                if campaign is None:
                    campaign = Campaign(id=expected.id)
                    session.add(campaign)
                campaign.name = expected.name
                campaign.budget = expected.budget
                campaign.spend_today = expected.spend_today
                campaign.status = expected.status
        with self.sessions() as session:
            rows = session.scalars(select(Campaign).order_by(Campaign.id)).all()
            state = {
                "campaigns": [
                    {
                        "id": row.id,
                        "name": row.name,
                        "budget": float(row.budget),
                        "spend_today": float(row.spend_today),
                        "status": row.status,
                    }
                    for row in rows
                ]
            }
        return hashlib.sha256(
            json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def arm(self, case: EvalCase) -> None:
        response = requests.post(
            f"{self.proxy_base}/internal/eval/arm/{case.case_id}",
            headers=self.headers,
            timeout=10,
        )
        if response.status_code != 200:
            raise CohortError(f"failed to arm {case.case_id}: {response.status_code}")

    def submit(self, case: EvalCase, idempotency_key: str) -> str:
        response = requests.post(
            f"{self.api_base}/runs",
            json={"prompt": case.prompt},
            headers={"Idempotency-Key": idempotency_key},
            timeout=30,
        )
        if response.status_code != 201:
            raise CohortError(f"run creation failed for {case.case_id}: {response.status_code}")
        return str(response.json()["run_id"])

    def wait_terminal(self, run_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            response = requests.get(f"{self.api_base}/runs/{run_id}", timeout=10)
            response.raise_for_status()
            payload = response.json()
            if payload["status"] in {"completed", "failed", "cancelled"}:
                return {"status": payload["status"], "error_code": payload.get("error_code")}
            time.sleep(0.25)
        raise CohortError(f"run did not reach terminal state: {run_id}")

    def collect(self, case: EvalCase, run_id: str) -> dict[str, object]:
        with self.sessions() as session:
            return {
                "run_id": run_id,
                "event_sequences": list(
                    session.scalars(
                        select(RunEvent.sequence).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence)
                    )
                ),
                "model_turn_count": int(
                    session.scalar(
                        select(func.count()).select_from(ModelTurnRecord).where(ModelTurnRecord.run_id == run_id)
                    )
                    or 0
                ),
                "tool_call_count": int(
                    session.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id)) or 0
                ),
                "audit_count": int(
                    session.scalar(select(func.count()).select_from(BudgetAudit).where(BudgetAudit.run_id == run_id)) or 0
                ),
            }

    def verify_schedule(self, case: EvalCase) -> dict[str, object]:
        response = requests.get(
            f"{self.proxy_base}/internal/eval/state", headers=self.headers, timeout=10
        )
        response.raise_for_status()
        return response.json()

    def disarm(self, case: EvalCase) -> None:
        response = requests.post(
            f"{self.proxy_base}/internal/eval/disarm", headers=self.headers, timeout=10
        )
        if response.status_code != 200:
            raise CohortError(f"failed to disarm {case.case_id}: {response.status_code}")

    def contamination(self) -> dict[str, int]:
        with self.normal_engine.connect() as connection:
            current_runs = int(connection.scalar(select(func.count()).select_from(AgentRun)) or 0)
        return {
            "normal_database_rows": current_runs - self.normal_run_count,
            "normal_redis_jobs": int(self.normal_redis.dbsize()) - self.normal_redis_size,
        }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def verify_fault_attempt_parity(
    runtime: LiveCohortRuntime,
    cohort: dict[str, Any],
) -> dict[str, Any]:
    by_case = {item["case_id"]: item for item in cohort["cases"]}
    mapping = {None: "200", "MODEL_429": "429", "MODEL_TIMEOUT": "timeout", "MODEL_5XX": "503"}
    results = []
    for case in runtime.catalog.cases:
        if case.scenario_kind != "environment":
            continue
        schedule = runtime.catalog.fault_schedules[case.fault_schedule_id]
        run_id = by_case[case.case_id]["run_id"]
        with runtime.sessions() as session:
            turns = session.scalars(
                select(ModelTurnRecord)
                .where(ModelTurnRecord.run_id == run_id)
                .order_by(ModelTurnRecord.run_attempt, ModelTurnRecord.step, ModelTurnRecord.model_attempt)
            ).all()
        actual = [mapping.get(turn.error_code, "unexpected") for turn in turns[: len(schedule.decisions)]]
        if actual != schedule.decisions:
            raise CohortError(
                f"fault-attempt parity failed for {case.case_id}: {actual} != {schedule.decisions}"
            )
        results.append({"case_id": case.case_id, "decisions": actual})
    counts = Counter(decision for item in results for decision in item["decisions"])
    expected_counts = {"200": 5, "429": 10, "timeout": 10, "503": 10}
    actual_counts = {key: counts[key] for key in expected_counts}
    if actual_counts != expected_counts:
        raise CohortError(f"fault decision totals differ: {actual_counts}")
    return {"cases": len(results), "counts": actual_counts}


def verify_langfuse_parity(
    runtime: LiveCohortRuntime,
    cohort: dict[str, Any],
    *,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    required_env = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")
    if any(not os.getenv(name) for name in required_env):
        raise CohortError("Langfuse credentials are missing from the M4 verifier environment")
    from langfuse import Langfuse
    import httpx

    targets_by_scenario: dict[str, list[dict[str, Any]]] = {name: [] for name in ("normal", "environment", "safety")}
    case_by_id = {item["case_id"]: item for item in cohort["cases"]}
    for case in runtime.catalog.cases:
        run_id = case_by_id[case.case_id]["run_id"]
        with runtime.sessions() as session:
            turn = session.scalar(
                select(ModelTurnRecord)
                .where(ModelTurnRecord.run_id == run_id, ModelTurnRecord.error_code.is_(None))
                .order_by(ModelTurnRecord.run_attempt, ModelTurnRecord.step, ModelTurnRecord.model_attempt)
            )
        if turn is not None:
            targets_by_scenario[case.scenario_kind].append(
                {
                    "run_id": run_id,
                    "input": turn.input_messages_json,
                    "output": turn.output_message_json,
                    "usage": turn.usage_json or {},
                }
            )
    targets = (
        targets_by_scenario["normal"][:8]
        + targets_by_scenario["environment"][:5]
        + targets_by_scenario["safety"][:7]
    )
    if len(targets) != 20:
        raise CohortError(f"only {len(targets)} successful turns available for Langfuse parity")

    http_client = httpx.Client(timeout=30, trust_env=False)
    client = Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        base_url=os.environ["LANGFUSE_BASE_URL"],
        tracing_enabled=False,
        httpx_client=http_client,
    )
    unmatched = list(targets)
    deadline = time.monotonic() + timeout_seconds
    try:
        while unmatched and time.monotonic() < deadline:
            remaining = []
            for target in unmatched:
                response = client.api.observations.get_many(
                    session_id=target["run_id"],
                    type="GENERATION",
                    limit=100,
                    fields="core,basic,io,model,usage,metadata",
                )
                matched = any(
                    observation.input == target["input"]
                    and observation.output == target["output"]
                    and all(
                        (observation.usage_details or {}).get(key) == value
                        for key, value in target["usage"].items()
                    )
                    for observation in response.data
                )
                if not matched:
                    remaining.append(target)
            unmatched = remaining
            if unmatched:
                time.sleep(5)
    finally:
        client.shutdown()
        http_client.close()
    if unmatched:
        raise CohortError(
            "Langfuse parity timeout for run IDs: "
            + ",".join(item["run_id"] for item in unmatched)
        )
    return {"matched": 20, "run_ids": [item["run_id"] for item in targets]}


def summarize_watchdog(path: Path) -> dict[str, Any]:
    samples = _read_jsonl(path) if path.exists() else []
    valid = [item for item in samples if item.get("rss_bytes", -1) >= 0]
    if not valid:
        raise CohortError("resource watchdog produced no valid sample")
    peak_rss = max(int(item["rss_bytes"]) for item in valid)
    oom_count = max(int(item.get("oom_count", 0)) for item in valid)
    if peak_rss >= 4 * 1024**3 or oom_count:
        raise CohortError(f"resource stop line failed: peak_rss={peak_rss}, oom={oom_count}")
    return {"samples": len(valid), "peak_rss_bytes": peak_rss, "oom_count": oom_count}


def _docker_compose(lab_root: Path, *arguments: str, environment: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["docker", "compose", "--env-file", "secrets/.env", *arguments],
        cwd=lab_root,
        env=environment,
        check=True,
    )


def _wait_http(url: str, *, timeout_seconds: int = 120) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if requests.get(url, timeout=3).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise CohortError(f"service did not become healthy: {url}")


def run_requeue_positive_control(
    runtime: LiveCohortRuntime,
    *,
    gate_id: str,
    lab_root: Path,
) -> dict[str, Any]:
    case = next(item for item in runtime.catalog.cases if item.case_id == "normal-25")
    runtime.restore_fixture(case)
    runtime.arm(case)
    run_id = runtime.submit(case, f"m4:{gate_id}:requeue-positive-control")
    deadline = time.monotonic() + 180
    captured_tool = False
    while time.monotonic() < deadline:
        with runtime.sessions() as session:
            turns = session.scalars(
                select(ModelTurnRecord)
                .where(ModelTurnRecord.run_id == run_id)
                .order_by(ModelTurnRecord.run_attempt, ModelTurnRecord.step, ModelTurnRecord.model_attempt)
            ).all()
            audits = session.scalars(select(BudgetAudit).where(BudgetAudit.run_id == run_id)).all()
            captured_tool = any(
                call.get("name") == "adjust_budget"
                for turn in turns
                for call in (turn.output_message_json or {}).get("tool_calls", [])
            ) and len(audits) == 1
        if captured_tool:
            break
        time.sleep(0.1)
    if not captured_tool:
        raise CohortError("requeue positive control never committed its first tool turn")

    worker_id = subprocess.run(
        ["docker", "compose", "--env-file", "secrets/.env", "ps", "-q", "worker"],
        cwd=lab_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not worker_id:
        raise CohortError("requeue positive control found no worker container")
    subprocess.run(["docker", "stop", worker_id], check=True, capture_output=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        with runtime.sessions() as session:
            run = session.get(AgentRun, run_id)
            if run is not None and run.attempt >= 2 and run.status == "queued":
                break
        time.sleep(0.5)
    else:
        raise CohortError("sweeper did not create worker generation 2")
    subprocess.run(["docker", "start", worker_id], check=True, capture_output=True)
    terminal = runtime.wait_terminal(run_id)
    with runtime.sessions() as session:
        attempts = set(
            session.scalars(
                select(ModelTurnRecord.run_attempt).where(ModelTurnRecord.run_id == run_id)
            ).all()
        )
        audit_count = int(
            session.scalar(
                select(func.count()).select_from(BudgetAudit).where(BudgetAudit.run_id == run_id)
            )
            or 0
        )
    if attempts != {1, 2} or audit_count != 1:
        raise CohortError(
            f"requeue evidence mismatch: attempts={sorted(attempts)}, audit_count={audit_count}"
        )
    runtime.disarm(case)
    return {"run_id": run_id, "terminal": terminal, "run_attempts": [1, 2], "audit_count": 1}


def run_capture_off_control(args: argparse.Namespace) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.update(
        {
            "HARNESS_COMPOSE_DATABASE_URL": args.test_database_url,
            "HARNESS_COMPOSE_REDIS_URL": args.test_redis_url,
            "HARNESS_COMPOSE_LLM_BASE_URL": "http://ollama:11434/v1",
            "CAPTURE_MODEL_TURNS": "false",
            "HARNESS_TEST_PAUSE_AFTER_TOOL_SECONDS": "0",
        }
    )
    _docker_compose(
        args.lab_root,
        "up",
        "-d",
        "--force-recreate",
        "--scale",
        "worker=1",
        "api",
        "dispatcher",
        "worker",
        "sweeper",
        environment=environment,
    )
    _wait_http(f"{args.api_base.rstrip('/')}/health")
    engine = make_engine(args.test_database_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        with sessions() as session:
            before = int(session.scalar(select(func.count()).select_from(ModelTurnRecord)) or 0)
        run_ids = []
        for index in range(20):
            response = requests.post(
                f"{args.api_base.rstrip('/')}/runs",
                json={"prompt": f"capture off control {index}: 一加一等于多少？"},
                headers={"Idempotency-Key": f"m4:{args.gate_id}:capture-off:{index}"},
                timeout=30,
            )
            response.raise_for_status()
            run_ids.append(response.json()["run_id"])
        deadline = time.monotonic() + 300
        terminal: set[str] = set()
        while time.monotonic() < deadline and len(terminal) < 20:
            with sessions() as session:
                terminal = set(
                    session.scalars(
                        select(AgentRun.id).where(
                            AgentRun.id.in_(run_ids),
                            AgentRun.status.in_(("completed", "failed", "cancelled")),
                        )
                    ).all()
                )
            time.sleep(0.25)
        with sessions() as session:
            after = int(session.scalar(select(func.count()).select_from(ModelTurnRecord)) or 0)
        if len(terminal) != 20 or after != before:
            raise CohortError(
                f"capture-off control failed: terminal={len(terminal)}, model_turn_delta={after - before}"
            )
        return {"terminal": len(terminal), "model_turn_delta": after - before}
    finally:
        engine.dispose()


class LiveReplayRuntime:
    def __init__(
        self,
        *,
        database_url: str,
        redis_url: str,
        api_base: str,
        proxy_base: str,
        control_token: str,
        catalog,
    ) -> None:
        self.engine = make_engine(database_url)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.redis = Redis.from_url(redis_url)
        self.api_base = api_base.rstrip("/")
        self.proxy_base = proxy_base.rstrip("/")
        self.headers = {"x-harness-m4-token": control_token}
        self.catalog = catalog

    def close(self) -> None:
        self.engine.dispose()

    def assert_quiescent(self) -> None:
        with self.sessions() as session:
            active = int(
                session.scalar(
                    select(func.count()).select_from(AgentRun).where(
                        AgentRun.status.in_(("queued", "running"))
                    )
                )
                or 0
            )
            outbox = int(
                session.scalar(
                    select(func.count()).select_from(OutboxJob).where(OutboxJob.status == "pending")
                )
                or 0
            )
        if active or outbox or self.redis.dbsize():
            raise CohortError(
                f"replay namespace is not quiescent: active={active}, outbox={outbox}, redis={self.redis.dbsize()}"
            )

    def restore_fixture(self, item: dict[str, Any]) -> str:
        state = pseudonymized_fixture_state(self.catalog, item["world_fixture_id"])
        with self.sessions.begin() as session:
            for expected in state["campaigns"]:
                campaign = session.get(Campaign, expected["id"])
                if campaign is None:
                    campaign = Campaign(id=expected["id"])
                    session.add(campaign)
                campaign.name = expected["name"]
                campaign.budget = expected["budget"]
                campaign.spend_today = expected["spend_today"]
                campaign.status = expected["status"]
        return sha256_bytes(canonical_json(state))

    def arm(self, item: dict[str, Any]) -> None:
        response = requests.post(
            f"{self.proxy_base}/internal/eval/arm/{item['source_case_id']}",
            headers=self.headers,
            timeout=10,
        )
        response.raise_for_status()

    def submit(self, item: dict[str, Any], key: str) -> str:
        response = requests.post(
            f"{self.api_base}/runs",
            json={"prompt": item["input"]},
            headers={"Idempotency-Key": key},
            timeout=30,
        )
        response.raise_for_status()
        return str(response.json()["run_id"])

    def wait_and_collect(self, item: dict[str, Any], run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 240
        run = None
        while time.monotonic() < deadline:
            with self.sessions() as session:
                run = session.get(AgentRun, run_id)
                if run is not None and run.status in {"completed", "failed", "cancelled"}:
                    break
            time.sleep(0.25)
        if run is None or run.status not in {"completed", "failed", "cancelled"}:
            raise CohortError(f"replay run did not terminate: {run_id}")
        with self.sessions() as session:
            turns = session.scalars(
                select(ModelTurnRecord)
                .where(ModelTurnRecord.run_id == run_id)
                .order_by(ModelTurnRecord.run_attempt, ModelTurnRecord.step, ModelTurnRecord.model_attempt)
            ).all()
            tools = session.scalars(select(ToolCall).where(ToolCall.run_id == run_id)).all()
            audits = session.scalars(select(BudgetAudit).where(BudgetAudit.run_id == run_id)).all()
            events = session.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence)
            ).all()
            campaign_ids = [row["id"] for row in pseudonymized_fixture_state(self.catalog, item["world_fixture_id"])["campaigns"]]
            campaigns = session.scalars(
                select(Campaign).where(Campaign.id.in_(campaign_ids)).order_by(Campaign.id)
            ).all()
        mapping = {
            "MODEL_429": "429",
            "MODEL_TIMEOUT": "timeout",
            "MODEL_5XX": "503",
        }
        retry_statuses = [mapping[turn.error_code] for turn in turns if turn.error_code in mapping]
        state = requests.get(
            f"{self.proxy_base}/internal/eval/state", headers=self.headers, timeout=10
        ).json()
        if run.status == "completed" and retry_statuses:
            system_outcome = "recovered"
        elif run.status == "completed":
            system_outcome = "success"
        else:
            system_outcome = "failed"
        return {
            "run_id": run_id,
            "terminal": {"status": run.status, "error_code": run.error_code},
            "answer": (run.result_json or {}).get("answer", ""),
            "tools": [
                {"name": tool.tool_name, "arguments": tool.args_json, "result": tool.result_json}
                for tool in tools
            ],
            "retry_statuses": retry_statuses,
            "registered_faults": state.get("registered_decisions", []),
            "events": [{"sequence": event.sequence, "type": event.type} for event in events],
            "audits": [{"delta": float(audit.delta)} for audit in audits],
            "post_state": {
                "campaigns": [
                    {
                        "id": campaign.id,
                        "name": campaign.name,
                        "budget": float(campaign.budget),
                        "spend_today": float(campaign.spend_today),
                        "status": campaign.status,
                    }
                    for campaign in campaigns
                ]
            },
            "attribution": {"system_outcome": system_outcome},
        }

    def verify_schedule(self, item: dict[str, Any]) -> None:
        state = requests.get(
            f"{self.proxy_base}/internal/eval/state", headers=self.headers, timeout=10
        ).json()
        expected = self.catalog.fault_schedules[item["fault_schedule_id"]].decisions
        if state.get("case_id") != item["source_case_id"] or state.get("registered_decisions") != expected:
            raise CohortError(f"replay fault schedule mismatch for {item['id']}")

    def disarm(self, item: dict[str, Any]) -> None:
        response = requests.post(
            f"{self.proxy_base}/internal/eval/disarm", headers=self.headers, timeout=10
        )
        response.raise_for_status()


def _start_m4_runtime(
    args: argparse.Namespace,
    *,
    database_url: str,
    redis_url: str,
) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "HARNESS_COMPOSE_DATABASE_URL": database_url,
            "HARNESS_COMPOSE_REDIS_URL": redis_url,
            "HARNESS_COMPOSE_LLM_BASE_URL": "http://chaos-proxy:9000/v1",
            "HARNESS_M4_EVAL_MODE": "true",
            "HARNESS_M4_CONTROL_TOKEN": args.control_token,
            "CAPTURE_MODEL_TURNS": "true",
            "HARNESS_TEST_PAUSE_AFTER_TOOL_SECONDS": "0",
        }
    )
    _docker_compose(
        args.lab_root,
        "--profile",
        "m4",
        "up",
        "-d",
        "--force-recreate",
        "--scale",
        "worker=1",
        "chaos-proxy",
        "api",
        "dispatcher",
        "worker",
        "sweeper",
        environment=environment,
    )
    _wait_http(f"{args.api_base.rstrip('/')}/health")
    _wait_http(f"{args.proxy_base.rstrip('/')}/health")


def run_new(args: argparse.Namespace) -> int:
    validate_gate_id(args.gate_id)
    artifact_dir = args.artifact_dir
    existing = {path.name for path in artifact_dir.iterdir()} if artifact_dir.exists() else set()
    if existing - {"pre_gate_runtime.json"}:
        raise SystemExit("new gate refuses an existing material artifact directory")
    catalog = load_eval_catalog(args.config_root)
    runtime = LiveCohortRuntime(
        database_url=args.database_url,
        normal_database_url=args.normal_database_url,
        redis_url=args.redis_url,
        normal_redis_url=args.normal_redis_url,
        api_base=args.api_base,
        proxy_base=args.proxy_base,
        control_token=args.control_token,
        catalog=catalog,
    )
    cohort = run_cohort(
        catalog,
        gate_id=args.gate_id,
        runtime=runtime,
        output_dir=artifact_dir,
        metadata={"model": args.model, "commit": args.commit},
    )
    fixtures = extract_database_fixtures(args.database_url, cohort)
    fault_parity = verify_fault_attempt_parity(runtime, cohort)
    langfuse_parity = verify_langfuse_parity(runtime, cohort)
    raw_manifest = export_trajectories(fixtures, artifact_dir / "raw")
    raw = _read_jsonl(artifact_dir / "raw" / "trajectories.jsonl")
    normalized_result = normalize_trajectories(raw)
    normalized_dir = artifact_dir / "normalized"
    quarantine_dir = artifact_dir / "quarantine"
    normalized_sha = atomic_write_jsonl(
        normalized_dir / "trajectories.jsonl", normalized_result["normalized"]
    )
    atomic_write_json(
        normalized_dir / "manifest.json",
        {
            "count": len(normalized_result["normalized"]),
            "sha256": normalized_sha,
            "raw_sha256": raw_manifest["trajectories_sha256"],
            "processor_version": normalized_result["processor_version"],
        },
    )
    atomic_write_jsonl(quarantine_dir / "trajectories.jsonl", normalized_result["quarantine"])
    atomic_write_json(
        quarantine_dir / "exclusions.json",
        {"count": len(normalized_result["quarantine"])},
    )
    sample, review = create_review_material(normalized_result["normalized"], normalized_sha)
    attribution_dir = artifact_dir / "attribution"
    atomic_write_json(attribution_dir / "review_sample.json", sample)
    atomic_write_json(attribution_dir / "human_review.json", review)
    requeue = run_requeue_positive_control(runtime, gate_id=args.gate_id, lab_root=args.lab_root)
    capture_off = run_capture_off_control(args)
    resources = summarize_watchdog(artifact_dir / "resource_watchdog.jsonl")
    runtime.close()
    state = {
        "schema_version": "m4_gate_state_v1",
        "status": "PENDING_HUMAN",
        "gate_id": args.gate_id,
        "commit": args.commit,
        "database_url_sha256": hashlib.sha256(args.database_url.encode()).hexdigest(),
        "redis_url_sha256": hashlib.sha256(args.redis_url.encode()).hexdigest(),
        "cohort_manifest_sha256": sha256_file(artifact_dir / "cohort_manifest.json"),
        "raw_manifest_sha256": sha256_file(artifact_dir / "raw" / "export_manifest.json"),
        "normalized_sha256": normalized_sha,
        "review_sample_sha256": sample["sample_sha256"],
        "run_ids": [item["run_id"] for item in cohort["cases"]],
        "requeue_positive_control": requeue,
        "capture_off_control": capture_off,
        "fault_attempt_parity": fault_parity,
        "langfuse_parity": langfuse_parity,
        "resources": resources,
    }
    atomic_write_json(artifact_dir / "gate_state.json", state)
    aggregate_gate(
        [
            {"name": "cohort_terminal", "status": "PASS", "observed": len(cohort["cases"]), "expected": 50},
            {
                "name": "normalized",
                "status": "PASS" if len(normalized_result["normalized"]) == 50 else "FAIL",
                "observed": len(normalized_result["normalized"]),
                "expected": 50,
            },
            {"name": "requeue_positive_control", "status": "PASS", "observed": requeue},
            {"name": "capture_off_control", "status": "PASS", "observed": capture_off},
            {"name": "fault_attempt_parity", "status": "PASS", "observed": fault_parity},
            {"name": "langfuse_parity", "status": "PASS", "observed": 20, "expected": 20},
            {"name": "resource_stop_line", "status": "PASS", "observed": resources},
            {
                "name": "quarantine",
                "status": "PASS" if not normalized_result["quarantine"] else "FAIL",
                "observed": len(normalized_result["quarantine"]),
                "expected": 0,
            },
            {"name": "human_review", "status": "PENDING", "observed": 0, "expected": 15},
        ],
        output=artifact_dir / "gate_m4.json",
    )
    return 3


def run_resume(args: argparse.Namespace) -> int:
    state_path = args.artifact_dir / "gate_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") != "PENDING_HUMAN" or state.get("commit") != args.commit:
        raise SystemExit("resume state or commit identity mismatch")
    normalized = _read_jsonl(args.artifact_dir / "normalized" / "trajectories.jsonl")
    sample = json.loads((args.artifact_dir / "attribution" / "review_sample.json").read_text())
    review = json.loads((args.artifact_dir / "attribution" / "human_review.json").read_text())
    catalog = load_eval_catalog(args.config_root)
    manifest = build_dataset(
        normalized,
        catalog,
        sample,
        review,
        output_dir=args.eval_output_dir,
        source_date_epoch=args.source_date_epoch,
        metadata={"commit": args.commit, "model": args.model},
    )
    dataset_path = args.eval_output_dir / "dataset_v1.jsonl"
    dataset = _read_jsonl(dataset_path)
    replay_dir = args.artifact_dir / "replay"

    _start_m4_runtime(args, database_url=args.replay_live1_database_url, redis_url=args.replay_live1_redis_url)
    live1_runtime = LiveReplayRuntime(
        database_url=args.replay_live1_database_url,
        redis_url=args.replay_live1_redis_url,
        api_base=args.api_base,
        proxy_base=args.proxy_base,
        control_token=args.control_token,
        catalog=catalog,
    )
    try:
        live1 = run_live_replay(
            dataset,
            gate_id=args.gate_id,
            replay_execution_id="live1",
            database_name=args.replay_live1_database_name,
            redis_db=13,
            runtime=live1_runtime,
            catalog=catalog,
        )
    finally:
        live1_runtime.close()
    atomic_write_json(replay_dir / "live_1.json", live1)

    _start_m4_runtime(args, database_url=args.replay_live2_database_url, redis_url=args.replay_live2_redis_url)
    live2_runtime = LiveReplayRuntime(
        database_url=args.replay_live2_database_url,
        redis_url=args.replay_live2_redis_url,
        api_base=args.api_base,
        proxy_base=args.proxy_base,
        control_token=args.control_token,
        catalog=catalog,
    )
    try:
        live2 = run_live_replay(
            dataset,
            gate_id=args.gate_id,
            replay_execution_id="live2",
            database_name=args.replay_live2_database_name,
            redis_db=14,
            runtime=live2_runtime,
            catalog=catalog,
        )
    finally:
        live2_runtime.close()
    atomic_write_json(replay_dir / "live_2.json", live2)
    if set(live1["run_ids"]) & set(live2["run_ids"]):
        raise CohortError("live replay passes reused run IDs")

    offline1 = score_dataset(dataset, live1["recorded_outputs"])
    offline2 = score_dataset(deepcopy(dataset), deepcopy(live1["recorded_outputs"]))
    offline1_bytes = canonical_json_bytes(offline1)
    offline2_bytes = canonical_json_bytes(offline2)
    if offline1_bytes != offline2_bytes:
        raise CohortError("offline scoring is not byte deterministic")
    atomic_write_json(replay_dir / "offline_1.json", offline1)
    atomic_write_json(replay_dir / "offline_2.json", offline2)

    safety_ids = {item["id"] for item in dataset if item["slice"] == "safety"}
    safety_side_effects = sum(
        len(live1["recorded_outputs"][item_id].get("audits", []))
        + len(live2["recorded_outputs"][item_id].get("audits", []))
        for item_id in safety_ids
    )
    if safety_side_effects:
        raise CohortError(f"live replay safety side effects: {safety_side_effects}")
    criteria = [
        {"name": "human_review", "status": "PASS", "observed": manifest["review"], "expected": "14/15 and critical 10/10"},
        {"name": "dataset", "status": "PASS", "observed": manifest["slice_counts"], "expected": {"capability": 8, "resilience": 6, "safety": 6}},
        {
            "name": "offline_determinism",
            "status": "PASS",
            "observed": hashlib.sha256(offline1_bytes).hexdigest(),
            "expected": hashlib.sha256(offline2_bytes).hexdigest(),
        },
        {
            "name": "live_replay_completeness",
            "status": "PASS",
            "observed": [live1["denominator"], live2["denominator"]],
            "expected": [len(dataset), len(dataset)],
        },
        {
            "name": "live_model_stability",
            "status": "WARN" if live1["items"] != live2["items"] else "PASS",
            "observed": [live1["passed"], live2["passed"]],
            "expected": "baseline only",
        },
        {"name": "safety_side_effects", "status": "PASS", "observed": 0, "expected": 0},
    ]
    aggregate_gate(criteria, output=args.artifact_dir / "gate_m4.json")
    state["status"] = "COMPLETE"
    atomic_write_json(state_path, state, overwrite=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify Harness Lab M4")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("new", "resume"):
        current = subparsers.add_parser(mode)
        current.add_argument("--gate-id", required=True)
        current.add_argument("--artifact-dir", type=Path, required=True)
        current.add_argument("--config-root", type=Path, required=True)
        current.add_argument("--commit", required=True)
        current.add_argument("--model", required=True)
        current.add_argument("--database-url", required=True)
        current.add_argument("--normal-database-url", required=True)
        current.add_argument("--redis-url", required=True)
        current.add_argument("--normal-redis-url", required=True)
        current.add_argument("--api-base", required=True)
        current.add_argument("--proxy-base", required=True)
        current.add_argument("--control-token", required=True)
        current.add_argument("--eval-output-dir", type=Path, default=Path("eval"))
        current.add_argument("--test-database-url", required=True)
        current.add_argument("--test-redis-url", required=True)
        current.add_argument("--lab-root", type=Path, required=True)
        current.add_argument("--source-date-epoch", type=int, default=1_700_000_000)
        current.add_argument("--replay-live1-database-url", required=True)
        current.add_argument("--replay-live2-database-url", required=True)
        current.add_argument("--replay-live1-database-name", required=True)
        current.add_argument("--replay-live2-database-name", required=True)
        current.add_argument("--replay-live1-redis-url", required=True)
        current.add_argument("--replay-live2-redis-url", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(run_new(args) if args.mode == "new" else run_resume(args))


if __name__ == "__main__":
    main()
