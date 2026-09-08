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
        assert idempotency_key == f"m4:gate_test:{case.case_id}"
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
        gate_id="gate_test",
        runtime=runtime,
        output_dir=tmp_path / "gate-test",
        metadata={"model": "test-model", "commit": "a" * 40},
    )

    assert len(manifest["cases"]) == 50
    for case in catalog.cases:
        names = [name for name, case_id in runtime.calls if case_id == case.case_id]
        expected = ["restore", "arm", "submit", "terminal", "collect", "disarm"]
        if case.scenario_kind == "environment":
            expected = ["restore", "arm", "submit", "terminal", "collect", "verify", "disarm"]
        assert names == expected
    assert manifest["contamination"] == {"normal_database_rows": 0, "normal_redis_jobs": 0}
    assert json.loads((tmp_path / "gate-test" / "cohort_manifest.json").read_text())["gate_id"] == "gate_test"


def test_cohort_stops_before_success_manifest_on_fixture_mismatch(tmp_path: Path) -> None:
    catalog = load_eval_catalog(LAB_ROOT / "config" / "eval")
    runtime = FakeRuntime()
    runtime.fixture_hash = lambda _case: "0" * 64

    with pytest.raises(CohortError, match="pre-state hash"):
        run_cohort(
            catalog,
            gate_id="gate_test",
            runtime=runtime,
            output_dir=tmp_path / "failed",
            metadata={},
        )

    assert not (tmp_path / "failed" / "cohort_manifest.json").exists()


def test_cohort_stops_on_safety_side_effect(tmp_path: Path) -> None:
    catalog = load_eval_catalog(LAB_ROOT / "config" / "eval")
    runtime = FakeRuntime()
    original_collect = runtime.collect

    def collect(case, run_id):
        evidence = original_collect(case, run_id)
        if case.case_id == "safety-01":
            evidence["audit_count"] = 1
        return evidence

    runtime.collect = collect
    with pytest.raises(CohortError, match="safety side effect"):
        run_cohort(
            catalog,
            gate_id="gate_test",
            runtime=runtime,
            output_dir=tmp_path / "unsafe",
            metadata={},
        )
    assert not (tmp_path / "unsafe" / "cohort_manifest.json").exists()


def test_atomic_json_has_canonical_bytes_and_refuses_overwrite(tmp_path: Path) -> None:
    destination = tmp_path / "artifact.json"
    digest = atomic_write_json(destination, {"z": 1, "中文": 2})

    assert destination.read_bytes() == b'{"z":1,"\xe4\xb8\xad\xe6\x96\x87":2}\n'
    assert len(digest) == 64
    with pytest.raises(FileExistsError):
        atomic_write_json(destination, {"z": 2})


def test_gate_id_is_bounded_for_database_and_artifact_safety() -> None:
    from scripts.run_m4_cohort import validate_gate_id

    assert validate_gate_id("gate1") == "gate1"
    for invalid in ("", "UPPER", "../escape", "a" * 25, "semi;colon", "gate-test"):
        with pytest.raises(CohortError):
            validate_gate_id(invalid)


def test_new_gate_allows_only_wrapper_snapshot_and_watchdog(tmp_path: Path) -> None:
    from scripts.verify_m4 import validate_new_artifact_dir

    (tmp_path / "pre_gate_runtime.json").write_text("{}", encoding="utf-8")
    (tmp_path / "resource_watchdog.jsonl").write_text("{}\n", encoding="utf-8")
    validate_new_artifact_dir(tmp_path)

    (tmp_path / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="existing material"):
        validate_new_artifact_dir(tmp_path)


def test_m4_gate_aggregator_marks_pending_and_fail_as_not_passed(tmp_path: Path) -> None:
    from scripts.verify_m4 import aggregate_gate

    pending = aggregate_gate(
        [{"name": "cohort", "status": "PASS"}, {"name": "human_review", "status": "PENDING"}],
        output=tmp_path / "pending.json",
    )
    failed = aggregate_gate(
        [{"name": "cohort", "status": "PASS"}, {"name": "privacy", "status": "FAIL"}],
        output=tmp_path / "failed.json",
    )

    assert pending["all_passed"] is False
    assert pending["status"] == "PENDING_HUMAN"
    assert failed["all_passed"] is False
    assert failed["status"] == "FAIL"


def test_resume_identity_checks_every_persisted_hash_and_run_id(tmp_path: Path) -> None:
    from scripts.verify_m4 import validate_resume_identity

    artifact_dir = tmp_path / "gate"
    (artifact_dir / "raw").mkdir(parents=True)
    (artifact_dir / "normalized").mkdir()
    (artifact_dir / "attribution").mkdir()
    cohort = {"cases": [{"run_id": f"run-{index}"} for index in range(50)]}
    atomic_write_json(artifact_dir / "cohort_manifest.json", cohort)
    atomic_write_json(artifact_dir / "raw" / "export_manifest.json", {"count": 50})
    normalized = artifact_dir / "normalized" / "trajectories.jsonl"
    normalized.write_text("{}\n", encoding="utf-8")
    sample = {"sample_sha256": "a" * 64}
    atomic_write_json(artifact_dir / "attribution" / "review_sample.json", sample)
    state = {
        "status": "PENDING_HUMAN",
        "gate_id": "gate1",
        "commit": "b" * 40,
        "database_url_sha256": __import__("hashlib").sha256(b"db").hexdigest(),
        "redis_url_sha256": __import__("hashlib").sha256(b"redis").hexdigest(),
        "cohort_manifest_sha256": __import__("hashlib").sha256(
            (artifact_dir / "cohort_manifest.json").read_bytes()
        ).hexdigest(),
        "raw_manifest_sha256": __import__("hashlib").sha256(
            (artifact_dir / "raw" / "export_manifest.json").read_bytes()
        ).hexdigest(),
        "normalized_sha256": __import__("hashlib").sha256(normalized.read_bytes()).hexdigest(),
        "review_sample_sha256": "a" * 64,
        "run_ids": [f"run-{index}" for index in range(50)],
    }

    validate_resume_identity(
        state,
        artifact_dir=artifact_dir,
        gate_id="gate1",
        commit="b" * 40,
        database_url="db",
        redis_url="redis",
    )
    state["normalized_sha256"] = "0" * 64
    with pytest.raises(SystemExit, match="normalized SHA"):
        validate_resume_identity(
            state,
            artifact_dir=artifact_dir,
            gate_id="gate1",
            commit="b" * 40,
            database_url="db",
            redis_url="redis",
        )


def test_finalize_requires_normal_runtime_restoration_before_pass(tmp_path: Path) -> None:
    from scripts.verify_m4 import finalize_after_restore

    artifact_dir = tmp_path / "gate"
    artifact_dir.mkdir()
    atomic_write_json(
        artifact_dir / "gate_m4.json",
        {
            "schema_version": "m4_gate_v1",
            "status": "PENDING_RESTORE",
            "all_passed": False,
            "criteria": [
                {"name": "human_review", "status": "PASS"},
                {"name": "normal_runtime_restored", "status": "PENDING"},
            ],
        },
    )
    atomic_write_json(
        artifact_dir / "gate_state.json",
        {"status": "PENDING_RESTORE", "gate_id": "gate1", "commit": "c" * 40},
    )
    inspect = [
        {
            "service": service,
            "environment": {
                "DATABASE_URL": "postgresql+psycopg://postgres:harness@postgres:5432/harness",
                "REDIS_URL": "redis://redis:6379/0",
                "LLM_BASE_URL": "http://ollama:11434/v1",
                "CAPTURE_MODEL_TURNS": "false",
            },
            "running": True,
        }
        for service in ("api", "dispatcher", "worker", "sweeper")
    ]
    atomic_write_json(artifact_dir / "post_restore_inspect_resume.json", inspect)
    atomic_write_json(artifact_dir / "post_restore_health_resume.json", {"status": "ok"})

    gate = finalize_after_restore(
        artifact_dir=artifact_dir, gate_id="gate1", commit="c" * 40
    )

    assert gate["all_passed"] is True
    assert json.loads((artifact_dir / "gate_state.json").read_text())["status"] == "COMPLETE"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0B", 0), ("1.5MiB", round(1.5 * 1024**2)), ("2 GiB", 2 * 1024**3)],
)
def test_watchdog_parses_docker_memory_units(value: str, expected: int) -> None:
    from scripts.m4_watchdog import parse_bytes

    assert parse_bytes(value) == expected


def test_resource_summary_rejects_any_container_restart(tmp_path: Path) -> None:
    from scripts.verify_m4 import summarize_watchdog

    path = tmp_path / "watchdog.jsonl"
    path.write_text(
        json.dumps({"rss_bytes": 1024, "oom_count": 0, "restart_count": 1}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CohortError, match="restart"):
        summarize_watchdog(path)


def test_new_gate_waits_for_api_and_proxy_health(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import verify_m4

    seen: list[str] = []
    monkeypatch.setattr(verify_m4, "_wait_http", seen.append)

    verify_m4.wait_for_m4_runtime("http://api:8000", "http://chaos-proxy:9000")

    assert seen == ["http://api:8000/health", "http://chaos-proxy:9000/health"]
