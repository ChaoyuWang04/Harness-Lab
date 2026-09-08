from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.eval.artifacts import atomic_write_json, atomic_write_jsonl, canonical_json_bytes


class ReconstructionError(RuntimeError):
    pass


def _arguments(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ReconstructionError("tool arguments are not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ReconstructionError("tool arguments are not an object")
    return parsed


def _validate_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(deepcopy(events), key=lambda item: item["sequence"])
    sequences = [int(item["sequence"]) for item in ordered]
    if sequences and sequences != list(range(sequences[0], sequences[0] + len(sequences))):
        raise ReconstructionError("run event sequence has a gap or duplicate")
    return ordered


def reconstruct_trajectory(
    *,
    case: dict[str, Any],
    run: dict[str, Any],
    model_turns: list[dict[str, Any]],
    events: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    audits: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered_events = _validate_events(events)
    ordered_turns = sorted(
        deepcopy(model_turns),
        key=lambda item: (item["run_attempt"], item["step"], item["model_attempt"]),
    )
    identities = [
        (int(item["run_attempt"]), int(item["step"]), int(item["model_attempt"]))
        for item in ordered_turns
    ]
    if len(identities) != len(set(identities)):
        raise ReconstructionError("duplicate model turn identity")
    if any(event["type"] == "step.model_call" for event in ordered_events) and not ordered_turns:
        raise ReconstructionError("model call event has no captured model turn")

    available_tools = [deepcopy(item) for item in tool_calls]
    matched_tools: set[int] = set()
    normalized_turns: list[dict[str, Any]] = []
    for turn in ordered_turns:
        output = turn.get("output_message_json")
        linked_calls: list[dict[str, Any]] = []
        if isinstance(output, dict):
            for call in output.get("tool_calls", []):
                arguments = _arguments(call["arguments"])
                matches = [
                    (index, item)
                    for index, item in enumerate(available_tools)
                    if index not in matched_tools
                    and int(item["step"]) == int(turn["step"])
                    and item["tool_name"] == call["name"]
                    and item["args_json"] == arguments
                ]
                if len(matches) > 1:
                    raise ReconstructionError("ambiguous tool lineage")
                if matches:
                    index, tool = matches[0]
                    matched_tools.add(index)
                    linked_calls.append(
                        {
                            "id": call["id"],
                            "name": call["name"],
                            "arguments": arguments,
                            "status": tool["status"],
                            "result": tool.get("result_json"),
                            "idempotency_key": tool["idempotency_key"],
                        }
                    )
                else:
                    linked_calls.append(
                        {
                            "id": call["id"],
                            "name": call["name"],
                            "arguments": arguments,
                            "status": "rejected_or_unexecuted",
                            "result": None,
                            "idempotency_key": None,
                        }
                    )
        normalized_turns.append(
            {
                "identity": [int(turn["run_attempt"]), int(turn["step"]), int(turn["model_attempt"])],
                "prompt_version": turn["prompt_version"],
                "input_messages": turn["input_messages_json"],
                "output_content": output.get("content") if isinstance(output, dict) else None,
                "tool_calls": linked_calls,
                "usage": turn.get("usage_json"),
                "error_code": turn.get("error_code"),
            }
        )

    if len(matched_tools) != len(available_tools):
        raise ReconstructionError("tool result has no matching model tool call")

    source = {
        "case": case,
        "run": run,
        "model_turns": model_turns,
        "events": events,
        "tool_calls": tool_calls,
        "audits": audits,
    }
    result: dict[str, Any] = {
        "schema_version": "raw_trajectory_v1",
        "run_id": run["id"],
        "case_id": case["case_id"],
        "scenario_kind": case["scenario_kind"],
        "world_fixture_id": case["world_fixture_id"],
        "fault_schedule_id": case["fault_schedule_id"],
        "prompt": run["input_json"]["prompt"],
        "status": run["status"],
        "result": run.get("result_json"),
        "error_code": run.get("error_code"),
        "events": ordered_events,
        "model_turns": normalized_turns,
        "retry_statuses": [
            item["error_code"] for item in normalized_turns if item["error_code"] is not None
        ],
        "audits": sorted(deepcopy(audits), key=lambda item: str(item.get("tool_call_key", ""))),
    }
    result["source_sha256"] = hashlib.sha256(canonical_json_bytes(source)).hexdigest()
    return result


def export_trajectories(fixtures: list[dict[str, Any]], output_dir: Path) -> dict[str, Any]:
    trajectories = [reconstruct_trajectory(**fixture) for fixture in fixtures]
    trajectories.sort(key=lambda item: (item["case_id"], item["run_id"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    data_path = output_dir / "trajectories.jsonl"
    manifest_path = output_dir / "export_manifest.json"
    digest = atomic_write_jsonl(data_path, trajectories)
    manifest = {
        "schema_version": "raw_export_manifest_v1",
        "count": len(trajectories),
        "run_ids": [item["run_id"] for item in trajectories],
        "trajectories_sha256": digest,
    }
    try:
        atomic_write_json(manifest_path, manifest)
    except Exception:
        data_path.unlink(missing_ok=True)
        raise
    os.chmod(data_path, 0o444)
    os.chmod(manifest_path, 0o444)
    return manifest
