from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any, Protocol

from app.eval.artifacts import canonical_json_bytes
from app.eval.catalog import EvalCatalog
from app.eval.catalog import canonical_json, sha256_bytes
from app.eval.dataset import pseudonymized_fixture_state


class ReplayError(RuntimeError):
    pass


def _resolve(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        try:
            current = current[int(part)] if isinstance(current, list) else current[part]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ReplayError(f"assertion path not found: {path}") from error
    return current


def _subset(observed: Any, expected: Any) -> bool:
    if isinstance(expected, dict) and isinstance(observed, dict):
        return all(key in observed and _subset(observed[key], value) for key, value in expected.items())
    if isinstance(expected, list) and isinstance(observed, list):
        return all(value in observed for value in expected)
    return observed == expected


def _continuous(events: list[dict[str, Any]]) -> bool:
    sequence = [int(event["sequence"]) for event in events]
    return not sequence or sequence == list(range(sequence[0], sequence[0] + len(sequence)))


def _evaluate(assertion: dict[str, Any], observed: dict[str, Any]) -> tuple[bool, Any]:
    operator = assertion["operator"]
    expected = assertion.get("expected")
    if operator == "equals":
        actual = _resolve(observed, expected["path"])
        return actual == expected["value"], actual
    if operator == "subset":
        actual = _resolve(observed, expected["path"])
        return _subset(actual, expected["value"]), actual
    if operator in {"required_facts", "answer_fact"}:
        facts = expected if isinstance(expected, list) else [expected]
        actual = str(observed.get("answer", ""))
        return all(str(fact) in actual for fact in facts), actual[:500]
    if operator == "ordered_retry_statuses":
        actual = observed.get("retry_statuses", [])
        return actual == expected, actual
    if operator == "registered_faults":
        actual = observed.get("registered_faults", [])
        return actual == expected, actual
    if operator == "terminal_status":
        actual = observed.get("terminal", {}).get("status")
        return actual == expected, actual
    if operator == "error_code":
        actual = observed.get("terminal", {}).get("error_code")
        return actual == expected, actual
    if operator == "terminal":
        actual = observed.get("terminal", {}).get("status") in {"completed", "failed", "cancelled"}
        return actual is bool(expected), actual
    if operator == "sse_sequence_continuous":
        actual = _continuous(observed.get("events", []))
        return actual is bool(expected), actual
    if operator == "audit_count":
        actual = len(observed.get("audits", []))
        return actual == expected, actual
    if operator == "audit_delta":
        actual = [float(item["delta"]) for item in observed.get("audits", [])]
        return float(expected) in actual, actual
    if operator == "forbidden_delta":
        actual = [float(item["delta"]) for item in observed.get("audits", [])]
        return float(expected) not in actual, actual
    if operator == "expected_post_state":
        actual = observed.get("post_state", {})
        return _subset(actual, expected), actual
    if operator == "tool_allowed":
        actual = [item.get("name") for item in observed.get("tools", [])]
        return expected in actual, actual
    if operator == "system_outcome":
        actual = observed.get("attribution", {}).get("system_outcome")
        return actual == expected, actual
    raise ReplayError(f"unknown assertion operator: {operator}")


def score_item(
    item_id: str,
    assertions: list[dict[str, Any]],
    observed: dict[str, Any],
) -> dict[str, Any]:
    results = []
    for assertion in assertions:
        passed, actual = _evaluate(assertion, observed)
        results.append(
            {
                "operator": assertion["operator"],
                "expected": assertion.get("expected"),
                "observed": actual,
                "passed": passed,
            }
        )
    return {"id": item_id, "passed": all(result["passed"] for result in results), "assertions": results}


def validate_dataset_assertion_contract(dataset: list[dict[str, Any]]) -> None:
    if any(
        not isinstance(item.get("expected_behavior"), dict)
        or item["expected_behavior"].get("assertions") != item.get("assertions")
        for item in dataset
    ):
        raise ReplayError("expected_behavior assertions differ from executable assertions")


def score_dataset(
    dataset: list[dict[str, Any]], outputs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    validate_dataset_assertion_contract(dataset)
    ids = [item["id"] for item in dataset]
    if len(ids) != len(set(ids)):
        raise ReplayError("duplicate dataset item ID")
    if set(ids) != set(outputs):
        raise ReplayError("output IDs do not match dataset IDs")
    ordered = sorted(dataset, key=lambda item: item["id"])
    results = [score_item(item["id"], item["assertions"], outputs[item["id"]]) for item in ordered]
    slices = Counter(item["slice"] for item in ordered)
    passed_by_slice = Counter(
        item["slice"]
        for item, result in zip(ordered, results, strict=True)
        if result["passed"]
    )
    return {
        "schema_version": "m4_replay_report_v1",
        "denominator": len(ordered),
        "passed": sum(result["passed"] for result in results),
        "slice_counts": dict(sorted(slices.items())),
        "slice_passed": {name: passed_by_slice[name] for name in sorted(slices)},
        "items": results,
    }


class ReplayRuntime(Protocol):
    def assert_quiescent(self) -> None: ...
    def restore_fixture(self, item: dict[str, Any]) -> str: ...
    def arm(self, item: dict[str, Any]) -> None: ...
    def submit(self, item: dict[str, Any], key: str) -> str: ...
    def wait_and_collect(self, item: dict[str, Any], run_id: str) -> dict[str, Any]: ...
    def verify_schedule(self, item: dict[str, Any]) -> None: ...
    def disarm(self, item: dict[str, Any]) -> None: ...


def run_live_replay(
    dataset: list[dict[str, Any]],
    *,
    gate_id: str,
    replay_execution_id: str,
    database_name: str,
    redis_db: int,
    runtime: ReplayRuntime,
    catalog: EvalCatalog,
) -> dict[str, Any]:
    if not database_name.startswith("harness_m4_replay_"):
        raise ReplayError("live replay requires an isolated replay database")
    if redis_db == 0:
        raise ReplayError("live replay refuses Redis DB 0")
    validate_dataset_assertion_contract(dataset)
    before = canonical_json_bytes(dataset)
    runtime.assert_quiescent()
    outputs: dict[str, dict[str, Any]] = {}
    run_ids: list[str] = []
    for item in dataset:
        fixture_sha = sha256_bytes(
            canonical_json(pseudonymized_fixture_state(catalog, item["world_fixture_id"]))
        )
        if runtime.restore_fixture(item) != fixture_sha:
            raise ReplayError(f"pre-state mismatch for {item['id']}")
        schedule_id = item["fault_schedule_id"]
        if schedule_id != "none":
            schedule = catalog.fault_schedules.get(schedule_id)
            if schedule is None or schedule.sha256 != item["fault_schedule_sha256"]:
                raise ReplayError(f"fault schedule mismatch for {item['id']}")
        runtime.arm(item)
        run_id = runtime.submit(
            item,
            f"m4-replay:{gate_id}:{replay_execution_id}:{item['source_case_id']}",
        )
        if run_id in run_ids:
            raise ReplayError("live replay produced a duplicate run ID")
        run_ids.append(run_id)
        outputs[item["id"]] = runtime.wait_and_collect(item, run_id)
        if schedule_id != "none":
            runtime.verify_schedule(item)
        runtime.disarm(item)
    if canonical_json_bytes(dataset) != before:
        raise ReplayError("live replay modified the dataset")
    report = score_dataset(dataset, outputs)
    report.update(
        {
            "gate_id": gate_id,
            "replay_execution_id": replay_execution_id,
            "database_name": database_name,
            "redis_db": redis_db,
            "run_ids": run_ids,
            "recorded_outputs": outputs,
        }
    )
    return report
