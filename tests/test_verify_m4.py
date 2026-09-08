from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.eval.artifacts import atomic_write_json  # noqa: E402
from app.eval.catalog import load_eval_catalog  # noqa: E402
from scripts.run_m4_cohort import CohortError, run_cohort  # noqa: E402


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.active_case: str | None = None

    def restore_fixture(self, case) -> str:
        self.calls.append(("restore", case.case_id))
        return self.fixture_hash(case)

    def fixture_hash(self, case) -> str:
        fixture = load_eval_catalog(LAB_ROOT / "config" / "eval").world_fixtures[
            case.world_fixture_id
        ]
        return fixture.pre_state_sha256

    def arm(self, case) -> None:
        self.calls.append(("arm", case.case_id))
        self.active_case = case.case_id

    def submit(self, case, idempotency_key: str) -> str:
        self.calls.append(("submit", case.case_id))
        assert idempotency_key == f"m4:gate-test:{case.case_id}"
        return f"run-{case.case_id}"

    def wait_terminal(self, run_id: str) -> dict[str, object]:
        case_id = run_id.removeprefix("run-")
        self.calls.append(("terminal", case_id))
        return {"status": "completed", "error_code": None}

    def collect(self, case, run_id: str) -> dict[str, object]:
        self.calls.append(("collect", case.case_id))
        return {"run_id": run_id, "events": [1], "model_turns": [1], "audits": []}

    def verify_schedule(self, case) -> dict[str, object]:
        self.calls.append(("verify", case.case_id))
        return {
            "case_id": case.case_id,
            "cursor": len(load_eval_catalog(LAB_ROOT / "config" / "eval").fault_schedules[case.case_id].decisions),
        }

    def disarm(self, case) -> None:
        self.calls.append(("disarm", case.case_id))
        self.active_case = None

    def contamination(self) -> dict[str, int]:
        return {"normal_database_rows": 0, "normal_redis_jobs": 0}


def test_cohort_orders_fixture_schedule_submission_and_terminal_collection(tmp_path: Path) -> None:
    catalog = load_eval_catalog(LAB_ROOT / "config" / "eval")
    runtime = FakeRuntime()

    manifest = run_cohort(
        catalog,
        gate_id="gate-test",
        runtime=runtime,
        output_dir=tmp_path / "gate-test",
        metadata={"model": "test-model", "commit": "a" * 40},
    )

    assert len(manifest["cases"]) == 50
    for case in catalog.cases:
        names = [name for name, case_id in runtime.calls if case_id == case.case_id]
        expected = ["restore", "submit", "terminal", "collect"]
        if case.scenario_kind == "environment":
            expected = ["restore", "arm", "submit", "terminal", "collect", "verify", "disarm"]
        assert names == expected
    assert manifest["contamination"] == {"normal_database_rows": 0, "normal_redis_jobs": 0}
    assert json.loads((tmp_path / "gate-test" / "cohort_manifest.json").read_text())["gate_id"] == "gate-test"


def test_cohort_stops_before_success_manifest_on_fixture_mismatch(tmp_path: Path) -> None:
    catalog = load_eval_catalog(LAB_ROOT / "config" / "eval")
    runtime = FakeRuntime()
    runtime.fixture_hash = lambda _case: "0" * 64

    with pytest.raises(CohortError, match="pre-state hash"):
        run_cohort(
            catalog,
            gate_id="gate-test",
            runtime=runtime,
            output_dir=tmp_path / "failed",
            metadata={},
        )

    assert not (tmp_path / "failed" / "cohort_manifest.json").exists()


def test_atomic_json_has_canonical_bytes_and_refuses_overwrite(tmp_path: Path) -> None:
    destination = tmp_path / "artifact.json"
    digest = atomic_write_json(destination, {"z": 1, "中文": 2})

    assert destination.read_bytes() == b'{"z":1,"\xe4\xb8\xad\xe6\x96\x87":2}\n'
    assert len(digest) == 64
    with pytest.raises(FileExistsError):
        atomic_write_json(destination, {"z": 2})
