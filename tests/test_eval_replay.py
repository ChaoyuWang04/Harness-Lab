from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.eval.catalog import load_eval_catalog  # noqa: E402
from app.eval.replay import ReplayError, run_live_replay, score_dataset, score_item  # noqa: E402


def _observed() -> dict[str, object]:
    return {
        "terminal": {"status": "completed", "error_code": None},
        "answer": "campaign_001 budget is 1000 and active",
        "tools": [{"name": "get_campaign", "arguments": {"campaign_id": "campaign_001"}}],
        "retry_statuses": ["429", "503"],
        "registered_faults": ["429", "503", "200"],
        "events": [{"sequence": 1}, {"sequence": 2}, {"sequence": 3}],
        "audits": [{"delta": 50.0}],
        "post_state": {"budget": 1050.0},
        "attribution": {"system_outcome": "recovered"},
    }


@pytest.mark.parametrize(
    "assertion",
    [
        {"operator": "equals", "expected": {"path": "terminal.status", "value": "completed"}},
        {"operator": "subset", "expected": {"path": "tools.0.arguments", "value": {"campaign_id": "campaign_001"}}},
        {"operator": "required_facts", "expected": ["campaign_001", "1000"]},
        {"operator": "ordered_retry_statuses", "expected": ["429", "503"]},
        {"operator": "terminal_status", "expected": "completed"},
        {"operator": "error_code", "expected": None},
        {"operator": "sse_sequence_continuous", "expected": True},
        {"operator": "audit_count", "expected": 1},
        {"operator": "audit_delta", "expected": 50.0},
        {"operator": "expected_post_state", "expected": {"budget": 1050.0}},
        {"operator": "tool_allowed", "expected": "get_campaign"},
        {"operator": "answer_fact", "expected": "active"},
        {"operator": "registered_faults", "expected": ["429", "503", "200"]},
        {"operator": "system_outcome", "expected": "recovered"},
        {"operator": "terminal", "expected": True},
        {"operator": "forbidden_delta", "expected": -1000.0},
    ],
)
def test_structured_assertion_operators(assertion: dict[str, object]) -> None:
    result = score_item("item-1", [assertion], _observed())
    assert result["passed"] is True
    assert result["assertions"][0]["passed"] is True


def test_unknown_assertion_operator_is_rejected() -> None:
    with pytest.raises(ReplayError, match="unknown assertion operator"):
        score_item("item-1", [{"operator": "llm_judge", "expected": True}], _observed())


def test_offline_report_has_one_verdict_per_item_and_is_deterministic() -> None:
    dataset = [
        {"id": "a", "slice": "capability", "assertions": [{"operator": "terminal", "expected": True}]},
        {"id": "b", "slice": "safety", "assertions": [{"operator": "audit_count", "expected": 1}]},
    ]
    outputs = {"a": _observed(), "b": _observed()}

    first = score_dataset(dataset, outputs)
    second = score_dataset(deepcopy(dataset), deepcopy(outputs))

    assert first == second
    assert len(first["items"]) == first["denominator"] == 2
    assert first["slice_counts"] == {"capability": 1, "safety": 1}


class FakeReplayRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def assert_quiescent(self) -> None:
        self.calls.append(("quiescent", "all"))

    def restore_fixture(self, item) -> str:
        self.calls.append(("restore", item["id"]))
        return item["pre_state_sha256"]

    def arm(self, item) -> None:
        self.calls.append(("arm", item["id"]))

    def submit(self, item, key: str) -> str:
        self.calls.append(("submit", item["id"]))
        assert key == f"m4-replay:gate1:live1:{item['source_case_id']}"
        return f"new-{item['id']}"

    def wait_and_collect(self, item, run_id: str) -> dict[str, object]:
        self.calls.append(("collect", item["id"]))
        observed = _observed()
        observed["run_id"] = run_id
        return observed

    def verify_schedule(self, item) -> None:
        self.calls.append(("verify", item["id"]))

    def disarm(self, item) -> None:
        self.calls.append(("disarm", item["id"]))


def test_live_replay_orders_fixture_schedule_and_uses_disjoint_execution_identity() -> None:
    catalog = load_eval_catalog(LAB_ROOT / "config" / "eval")
    dataset = [
        {
            "id": "m4-env-01",
            "source_case_id": "env-01",
            "slice": "resilience",
            "assertions": [{"operator": "terminal", "expected": True}],
            "pre_state_sha256": catalog.world_fixtures["campaigns-v1"].pre_state_sha256,
            "world_fixture_id": "campaigns-v1",
            "fault_schedule_id": "env-01",
            "fault_schedule_sha256": catalog.fault_schedules["env-01"].sha256,
        }
    ]
    runtime = FakeReplayRuntime()

    report = run_live_replay(
        dataset,
        gate_id="gate1",
        replay_execution_id="live1",
        database_name="harness_m4_replay_live1_gate1",
        redis_db=13,
        runtime=runtime,
        catalog=catalog,
    )

    assert runtime.calls == [
        ("quiescent", "all"),
        ("restore", "m4-env-01"),
        ("arm", "m4-env-01"),
        ("submit", "m4-env-01"),
        ("collect", "m4-env-01"),
        ("verify", "m4-env-01"),
        ("disarm", "m4-env-01"),
    ]
    assert report["run_ids"] == ["new-m4-env-01"]


@pytest.mark.parametrize(
    ("database_name", "redis_db"),
    [("harness", 13), ("harness_m4_replay_gate1", 0)],
)
def test_live_replay_refuses_normal_database_or_redis(database_name: str, redis_db: int) -> None:
    with pytest.raises(ReplayError):
        run_live_replay(
            [],
            gate_id="gate1",
            replay_execution_id="live1",
            database_name=database_name,
            redis_db=redis_db,
            runtime=FakeReplayRuntime(),
            catalog=load_eval_catalog(LAB_ROOT / "config" / "eval"),
        )
