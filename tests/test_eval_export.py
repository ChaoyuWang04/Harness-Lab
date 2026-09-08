from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.eval.export import ReconstructionError, export_trajectories, reconstruct_trajectory  # noqa: E402


def _fixture() -> dict[str, object]:
    return {
        "case": {
            "case_id": "normal-01",
            "scenario_kind": "normal",
            "world_fixture_id": "campaigns-v1",
            "fault_schedule_id": "none",
        },
        "run": {
            "id": "run-1",
            "status": "completed",
            "input_json": {"prompt": "查询 camp_001"},
            "result_json": {"answer": "预算为 1000"},
            "error_code": None,
            "prompt_version": "v1",
        },
        "model_turns": [
            {
                "run_id": "run-1",
                "run_attempt": 1,
                "step": 1,
                "model_attempt": 0,
                "prompt_version": "v1",
                "input_messages_json": [{"role": "user", "content": "查询 camp_001"}],
                "output_message_json": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "name": "get_campaign",
                            "arguments": '{"campaign_id":"camp_001"}',
                        }
                    ],
                },
                "usage_json": {"total": 10},
                "error_code": None,
            },
            {
                "run_id": "run-1",
                "run_attempt": 1,
                "step": 2,
                "model_attempt": 0,
                "prompt_version": "v1",
                "input_messages_json": [{"role": "tool", "tool_call_id": "call-1", "content": "{}"}],
                "output_message_json": {"content": "预算为 1000", "tool_calls": []},
                "usage_json": {"total": 5},
                "error_code": None,
            },
        ],
        "events": [
            {"sequence": 1, "type": "run.created", "payload": {}},
            {"sequence": 2, "type": "run.started", "payload": {"attempt": 1}},
            {"sequence": 3, "type": "step.model_call", "payload": {"step": 1}},
            {"sequence": 4, "type": "step.tool_call", "payload": {"step": 1, "tool_name": "get_campaign"}},
            {"sequence": 5, "type": "step.tool_result", "payload": {"step": 1, "tool_name": "get_campaign", "result": {"budget": 1000}}},
            {"sequence": 6, "type": "step.model_call", "payload": {"step": 2}},
            {"sequence": 7, "type": "run.completed", "payload": {"answer": "预算为 1000"}},
        ],
        "tool_calls": [
            {
                "step": 1,
                "tool_name": "get_campaign",
                "args_json": {"campaign_id": "camp_001"},
                "status": "succeeded",
                "result_json": {"budget": 1000},
                "idempotency_key": "key-1",
            }
        ],
        "audits": [],
    }


def test_reconstructs_tool_lineage_and_model_order_deterministically() -> None:
    fixture = _fixture()
    trajectory = reconstruct_trajectory(**fixture)

    assert trajectory["run_id"] == "run-1"
    assert trajectory["model_turns"][0]["identity"] == [1, 1, 0]
    assert trajectory["model_turns"][0]["tool_calls"][0] == {
        "id": "call-1",
        "name": "get_campaign",
        "arguments": {"campaign_id": "camp_001"},
        "status": "succeeded",
        "result": {"budget": 1000},
        "idempotency_key": "key-1",
    }
    assert trajectory == reconstruct_trajectory(**deepcopy(fixture))
    assert len(trajectory["source_sha256"]) == 64


def test_reconstructs_retry_and_worker_generation_order() -> None:
    fixture = _fixture()
    fixture["model_turns"] = [
        {**fixture["model_turns"][0], "error_code": "MODEL_429", "output_message_json": None},
        {**fixture["model_turns"][0], "model_attempt": 1},
        {**fixture["model_turns"][1], "run_attempt": 2, "step": 1},
    ]
    fixture["events"] = [
        {"sequence": 1, "type": "step.model_call", "payload": {"step": 1}},
        {"sequence": 2, "type": "step.model_retry", "payload": {"step": 1, "error_code": "MODEL_429"}},
    ]

    trajectory = reconstruct_trajectory(**fixture)

    assert [turn["identity"] for turn in trajectory["model_turns"]] == [
        [1, 1, 0],
        [1, 1, 1],
        [2, 1, 0],
    ]
    assert trajectory["retry_statuses"] == ["MODEL_429"]


@pytest.mark.parametrize("mutation", ["missing_turn", "duplicate_turn", "event_gap", "tool_conflict"])
def test_reconstruction_rejects_incomplete_or_conflicting_evidence(mutation: str) -> None:
    fixture = _fixture()
    if mutation == "missing_turn":
        fixture["model_turns"] = []
    elif mutation == "duplicate_turn":
        fixture["model_turns"].append(deepcopy(fixture["model_turns"][0]))
    elif mutation == "event_gap":
        fixture["events"][1]["sequence"] = 3
    else:
        fixture["tool_calls"][0]["args_json"] = {"campaign_id": "camp_999"}

    with pytest.raises(ReconstructionError):
        reconstruct_trajectory(**fixture)


def test_export_failure_leaves_no_success_manifest(tmp_path: Path) -> None:
    fixture = _fixture()
    fixture["model_turns"] = []

    with pytest.raises(ReconstructionError):
        export_trajectories([fixture], tmp_path / "raw")

    assert not (tmp_path / "raw" / "export_manifest.json").exists()


def test_export_is_byte_deterministic_and_read_only(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_manifest = export_trajectories([_fixture()], first)
    second_manifest = export_trajectories([_fixture()], second)

    assert first_manifest["trajectories_sha256"] == second_manifest["trajectories_sha256"]
    assert (first / "trajectories.jsonl").read_bytes() == (second / "trajectories.jsonl").read_bytes()
    assert json.loads((first / "export_manifest.json").read_text())["count"] == 1
    assert (first / "trajectories.jsonl").stat().st_mode & 0o222 == 0
