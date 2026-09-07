from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


LAB_ROOT = Path(__file__).resolve().parents[1]
VERIFIER = LAB_ROOT / "scripts" / "verify_m3.py"


def load_verifier():
    spec = importlib.util.spec_from_file_location("verify_m3", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registered_experiment_sizes_are_fixed() -> None:
    verifier = load_verifier()

    assert verifier.EXPERIMENT_SIZES == {
        "worker_crashes": 10,
        "redis_outage_runs": 60,
        "provider_arm_runs": 30,
        "load_arm_runs": 500,
        "load_concurrency": 50,
        "sse_clients": 20,
        "sse_reconnects": 5,
    }


def test_m3_tool_arms_reuse_the_verified_exact_campaign_instruction() -> None:
    verifier = load_verifier()
    source = VERIFIER.read_text(encoding="utf-8")

    assert verifier.EXACT_CAMPAIGN_INSTRUCTION == (
        "调用工具时必须原样保留标识符，campaign_id 必须是精确字符串 camp_001，"
        "不得省略 camp_ 前缀。"
    )
    assert source.count("EXACT_CAMPAIGN_INSTRUCTION") >= 3


def test_tool_contract_preflight_requires_three_exact_calls() -> None:
    verifier = load_verifier()
    calls = [
        {"tool_name": "adjust_budget", "campaign_id": "camp_001", "delta": 1}
        for _ in range(3)
    ]

    assert verifier.validate_tool_contract(calls)["ok"] is True
    with pytest.raises(AssertionError, match="3/3"):
        verifier.validate_tool_contract([{**calls[0], "campaign_id": "001"}, *calls[1:]])
    with pytest.raises(AssertionError, match="3/3"):
        verifier.validate_tool_contract(
            [{"tool_name": "adjust_budget", "campaign_id": "camp_001"}, *calls[1:]]
        )


def test_summarize_run_timings_uses_event_timestamps() -> None:
    verifier = load_verifier()
    origin = datetime(2026, 9, 7, tzinfo=timezone.utc)
    runs = [
        {"created_at": origin, "started_at": origin + timedelta(seconds=2), "terminal_at": origin + timedelta(seconds=5), "status": "completed"},
        {"created_at": origin, "started_at": origin + timedelta(seconds=4), "terminal_at": origin + timedelta(seconds=10), "status": "failed"},
    ]

    result = verifier.summarize_run_timings(runs)

    assert result["run_p95_seconds"] == pytest.approx(5.85)
    assert result["queue_lag_p95_seconds"] == pytest.approx(3.9)
    assert result["failed_rate"] == 0.5


@pytest.mark.parametrize(
    ("raw", "expected"),
    (("132.4MiB / 256MiB", 132.4), ("1.5 GiB / 4 GiB", 1536.0), ("512kB / 1GB", 0.512)),
)
def test_parse_docker_memory_supports_compact_and_spaced_units(
    raw: str, expected: float
) -> None:
    verifier = load_verifier()

    assert verifier.parse_memory_to_mib(raw) == pytest.approx(expected)


def test_worker_crash_gate_requires_ten_injections_and_no_duplicate_audits() -> None:
    verifier = load_verifier()
    trials = [
        {"injected": True, "status": "completed", "recovery_seconds": 39, "audit_rows": 1}
        for _ in range(10)
    ]

    assert verifier.validate_worker_crashes(trials)["ok"] is True
    with pytest.raises(AssertionError, match="ten"):
        verifier.validate_worker_crashes(trials[:-1])
    with pytest.raises(AssertionError, match="duplicate"):
        verifier.validate_worker_crashes([{**trials[0], "audit_rows": 2}, *trials[1:]])


def test_load_gate_requires_post_latency_and_fifty_percent_queue_improvement() -> None:
    verifier = load_verifier()
    one = {"count": 500, "post_p95_ms": 80, "queue_lag_p95_seconds": 100}
    four = {"count": 500, "post_p95_ms": 90, "queue_lag_p95_seconds": 40}

    assert verifier.validate_load_arms(one, four)["ok"] is True
    with pytest.raises(AssertionError, match="50%"):
        verifier.validate_load_arms(one, {**four, "queue_lag_p95_seconds": 60})


def test_sse_gate_requires_exact_sequences_without_duplicates() -> None:
    verifier = load_verifier()
    clients = [
        {"reconnects": 5, "received": [1, 2, 3, 4, 5], "database": [1, 2, 3, 4, 5]}
        for _ in range(20)
    ]

    assert verifier.validate_sse_clients(clients)["ok"] is True
    with pytest.raises(AssertionError, match="sequence"):
        verifier.validate_sse_clients([{**clients[0], "received": [1, 2, 2, 4, 5]}, *clients[1:]])


def test_gate_requires_six_experiments_zero_duplicates_and_complete_table() -> None:
    verifier = load_verifier()
    experiments = {f"EXP-{index}": {"ok": True} for index in range(1, 7)}
    table = {
        metric: {arm: 1.0 for arm in ("normal", "429", "load_one", "load_four")}
        for metric in ("run_p95_seconds", "queue_lag_p95_seconds", "failed_rate")
    }

    result = verifier.validate_gate(experiments, duplicate_audit_keys=0, comparison=table)

    assert result["all_passed"] is True
    with pytest.raises(AssertionError, match="duplicate"):
        verifier.validate_gate(experiments, duplicate_audit_keys=1, comparison=table)


def test_evidence_writer_rejects_secrets(tmp_path: Path) -> None:
    verifier = load_verifier()
    output = tmp_path / "gate.json"

    verifier.write_redacted_evidence(output, {"all_passed": True}, ["secret-value"])
    assert json.loads(output.read_text())["all_passed"] is True
    with pytest.raises(AssertionError, match="secret"):
        verifier.write_redacted_evidence(output, {"value": "secret-value"}, ["secret-value"])


def test_run_event_records_retain_timestamps_for_recovery_measurement() -> None:
    source = (LAB_ROOT / "scripts" / "verify_m3.py").read_text(encoding="utf-8")

    assert '"created_at": event.created_at' in source


def test_compose_failures_include_captured_diagnostics() -> None:
    source = (LAB_ROOT / "scripts" / "verify_m3.py").read_text(encoding="utf-8")

    assert "M3 Compose command failed" in source
    assert "result.stderr" in source
