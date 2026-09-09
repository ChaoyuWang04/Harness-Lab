from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from typing import Any, Protocol

from app.eval.artifacts import atomic_write_json
from app.eval.catalog import EvalCase, EvalCatalog, load_eval_catalog


GATE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,23}$")


class CohortError(RuntimeError):
    pass


class CohortRuntime(Protocol):
    def restore_fixture(self, case: EvalCase) -> str: ...
    def arm(self, case: EvalCase) -> None: ...
    def submit(self, case: EvalCase, idempotency_key: str) -> str: ...
    def wait_terminal(self, run_id: str) -> dict[str, object]: ...
    def collect(self, case: EvalCase, run_id: str) -> dict[str, object]: ...
    def verify_schedule(self, case: EvalCase) -> dict[str, object]: ...
    def disarm(self, case: EvalCase) -> None: ...
    def contamination(self) -> dict[str, int]: ...


def validate_gate_id(gate_id: str) -> str:
    if not GATE_ID_PATTERN.fullmatch(gate_id):
        raise CohortError("gate_id must be 1-24 lowercase safe characters")
    return gate_id


def _verify_schedule(case: EvalCase, catalog: EvalCatalog, state: dict[str, object]) -> None:
    schedule = catalog.fault_schedules[case.fault_schedule_id]
    if state.get("case_id") != case.case_id:
        raise CohortError(f"cross-case fault consumption for {case.case_id}")
    if state.get("cursor") != len(schedule.decisions):
        raise CohortError(f"fault schedule not fully consumed for {case.case_id}")
    counts = state.get("registered_counts")
    if isinstance(counts, dict):
        expected = {
            status: schedule.decisions.count(status)
            for status in ("200", "429", "timeout", "503")
        }
        if counts != expected:
            raise CohortError(f"fault schedule counts differ for {case.case_id}")


def run_cohort(
    catalog: EvalCatalog,
    *,
    gate_id: str,
    runtime: CohortRuntime,
    output_dir: Path,
    metadata: dict[str, object],
) -> dict[str, Any]:
    validate_gate_id(gate_id)
    manifest_path = output_dir / "cohort_manifest.json"
    if manifest_path.exists():
        raise CohortError("completed cohort manifest already exists")

    case_records: list[dict[str, object]] = []
    for case in catalog.cases:
        expected_pre_state = catalog.world_fixtures[case.world_fixture_id].pre_state_sha256
        observed_pre_state = runtime.restore_fixture(case)
        if observed_pre_state != expected_pre_state:
            raise CohortError(f"pre-state hash mismatch for {case.case_id}")

        runtime.arm(case)
        run_id = runtime.submit(case, f"m4:{gate_id}:{case.case_id}")
        terminal = runtime.wait_terminal(run_id)
        if terminal.get("status") not in {"completed", "failed", "cancelled"}:
            raise CohortError(f"missing terminal status for {case.case_id}")
        evidence = runtime.collect(case, run_id)
        if case.scenario_kind == "safety" and int(evidence.get("audit_count", 0)) != 0:
            raise CohortError(f"safety side effect detected for {case.case_id}")

        schedule_state: dict[str, object] | None = None
        if case.scenario_kind == "environment":
            schedule_state = runtime.verify_schedule(case)
            _verify_schedule(case, catalog, schedule_state)
        runtime.disarm(case)

        case_records.append(
            {
                "case_id": case.case_id,
                "run_id": run_id,
                "scenario_kind": case.scenario_kind,
                "input_sha256": hashlib.sha256(case.prompt.encode("utf-8")).hexdigest(),
                "world_fixture_id": case.world_fixture_id,
                "pre_state_sha256": observed_pre_state,
                "fault_schedule_id": case.fault_schedule_id,
                "expected_behavior": case.expected_behavior.model_dump(mode="json"),
                "terminal": terminal,
                "schedule_state": schedule_state,
                "evidence": evidence,
            }
        )

    contamination = runtime.contamination()
    if any(contamination.values()):
        raise CohortError(f"normal runtime contamination detected: {contamination}")

    manifest: dict[str, Any] = {
        "schema_version": "m4_cohort_manifest_v1",
        "gate_id": gate_id,
        "catalog_sha256": catalog.source_sha256,
        "metadata": metadata,
        "cases": case_records,
        "contamination": contamination,
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the registered M4 cohort")
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--gate-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    load_eval_catalog(args.config_root)
    validate_gate_id(args.gate_id)
    raise SystemExit("live runtime adapter is provided by the home5090 M4 gate wrapper")


if __name__ == "__main__":
    main()
