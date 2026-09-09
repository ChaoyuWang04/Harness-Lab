from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.eval.artifacts import canonical_json_bytes  # noqa: E402
from app.eval.catalog import load_eval_catalog  # noqa: E402
from app.eval.dataset import (  # noqa: E402
    HumanReviewError,
    build_dataset,
    create_review_material,
    validate_human_review,
    validate_dataset_manifest_contract,
    pseudonymized_fixture_state,
)


def _normalized() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for index in range(1, 31):
        items.append(_item(f"normal-{index:02d}", "normal", "positive", "correct", "none"))
    for index in range(1, 11):
        items.append(
            _item(f"env-{index:02d}", "environment", "resilience_eval", "correct", f"env-{index:02d}")
        )
    for index in range(1, 11):
        items.append(_item(f"safety-{index:02d}", "safety", "positive", "safe_refusal", "none"))
    return items


def _item(case_id: str, scenario: str, eligibility: str, behavior: str, schedule: str) -> dict[str, object]:
    return {
        "schema_version": "trajectory_v1",
        "processor_version": "m4-redactor-v1",
        "run_id": f"run-{case_id}",
        "case_id": case_id,
        "scenario_kind": scenario,
        "messages": [
            {"role": "system", "content": "test-system-prompt"},
            {"role": "user", "content": f"prompt {case_id}"},
        ],
        "terminal": {"status": "completed", "error_code": None},
        "attribution": {
            "system_outcome": "recovered" if scenario == "environment" else "success",
            "failure_owner": "environment" if scenario == "environment" else "none",
            "behavior_label": behavior,
            "dataset_eligibility": eligibility,
        },
        "lineage": {
            "source_sha256": hashlib.sha256(case_id.encode()).hexdigest(),
            "world_fixture_id": "campaigns-v1",
            "fault_schedule_id": schedule,
        },
    }


def _approved_review(items: list[dict[str, object]], sample: dict[str, object]) -> dict[str, object]:
    by_case = {item["case_id"]: item for item in items}
    decisions = []
    for sampled in sample["items"]:
        attribution = by_case[sampled["case_id"]]["attribution"]
        decisions.append({"case_id": sampled["case_id"], **attribution})
    return {"sample_sha256": sample["sample_sha256"], "decisions": decisions}


def _provenance() -> dict[str, object]:
    return {
        "prompt_version": "v1",
        "raw_trajectories_sha256": "1" * 64,
        "raw_export_manifest_sha256": "2" * 64,
        "normalized_trajectories_sha256": "3" * 64,
        "normalized_manifest_sha256": "4" * 64,
        "quarantine_manifest_sha256": "5" * 64,
        "quarantine_count": 0,
    }


def test_review_is_stratified_blinded_and_sha_bound() -> None:
    items = _normalized()
    normalized_sha = hashlib.sha256(canonical_json_bytes(items)).hexdigest()

    sample, template = create_review_material(items, normalized_sha)

    assert [item["scenario_kind"] for item in sample["items"]].count("normal") == 5
    assert [item["scenario_kind"] for item in sample["items"]].count("environment") == 5
    assert [item["scenario_kind"] for item in sample["items"]].count("safety") == 5
    assert "attribution" not in json.dumps(sample)
    assert all(decision["decision"] is None for decision in template["decisions"])
    assert template["sample_sha256"] == sample["sample_sha256"]


def test_incomplete_or_wrong_critical_review_is_rejected() -> None:
    items = _normalized()
    sample, _ = create_review_material(items, "a" * 64)
    review = _approved_review(items, sample)
    review["decisions"][0]["behavior_label"] = "bad_output"
    critical = next(
        decision
        for decision in review["decisions"]
        if decision["case_id"].startswith(("env-", "safety-"))
    )
    critical["dataset_eligibility"] = "behavior_negative"

    with pytest.raises(HumanReviewError, match="critical boundary"):
        validate_human_review(items, sample, review)


def test_dataset_meets_slices_lineage_and_is_byte_deterministic(tmp_path: Path) -> None:
    catalog = load_eval_catalog(LAB_ROOT / "config" / "eval")
    items = _normalized()
    sample, _ = create_review_material(items, "b" * 64)
    review = _approved_review(items, sample)

    first = build_dataset(
        items,
        catalog,
        sample,
        review,
        output_dir=tmp_path / "first",
        source_date_epoch=1_700_000_000,
        metadata={"commit": "a" * 40, "model": "test-model"},
        provenance=_provenance(),
    )
    second = build_dataset(
        deepcopy(items),
        catalog,
        deepcopy(sample),
        deepcopy(review),
        output_dir=tmp_path / "second",
        source_date_epoch=1_700_000_000,
        metadata={"commit": "a" * 40, "model": "test-model"},
        provenance=deepcopy(_provenance()),
    )

    assert first == second
    assert first["count"] >= 20
    assert first["slice_counts"] == {"capability": 8, "resilience": 6, "safety": 6}
    assert first["schemas"].keys() == {"dataset", "trajectory"}
    assert all(len(item["sha256"]) == 64 for item in first["schemas"].values())
    assert first["prompt"] == {
        "version": "v1",
        "sha256": hashlib.sha256(b"test-system-prompt").hexdigest(),
    }
    assert first["source_artifacts"] == {
        key: value for key, value in _provenance().items() if key.endswith("sha256")
    }
    assert first["counts"] == {
        "normalized": 50,
        "eligible": 50,
        "selected": 20,
        "not_selected": 30,
        "quarantined": 0,
    }
    assert first["exclusions"] == {
        "count": 30,
        "by_reason": {
            "ineligible": 0,
            "not_selected_fixed_slice_capacity": 30,
            "quarantined": 0,
        },
    }
    assert len(first["redactor_sha256"]) == 64
    assert (tmp_path / "first" / "dataset_v1.jsonl").read_bytes() == (
        tmp_path / "second" / "dataset_v1.jsonl"
    ).read_bytes()
    assert (tmp_path / "first" / "dataset_v1.manifest.json").read_bytes() == (
        tmp_path / "second" / "dataset_v1.manifest.json"
    ).read_bytes()
    rows = [json.loads(line) for line in (tmp_path / "first" / "dataset_v1.jsonl").read_text().splitlines()]
    assert len({row["id"] for row in rows}) == len(rows)
    assert all(row["assertions"] for row in rows)
    assert all(isinstance(row["expected_behavior"], dict) for row in rows)
    assert all(row["expected_behavior"]["assertions"] == row["assertions"] for row in rows)
    assert all(len(row["source_sha256"]) == 64 for row in rows)
    for row in rows:
        operators = {assertion["operator"] for assertion in row["assertions"]}
        assert "sse_sequence_continuous" in operators
        assert "expected_post_state" in operators
    assert validate_dataset_manifest_contract(first, rows, items, _provenance()) == {
        "dataset_items": 20,
        "excluded_items": 30,
        "schema_files": 2,
        "source_artifacts": 5,
        "structured_expected_behavior": 20,
    }
    incomplete = deepcopy(first)
    incomplete.pop("prompt")
    with pytest.raises(ValueError, match="manifest prompt"):
        validate_dataset_manifest_contract(incomplete, rows, items, _provenance())

    wrong_redactor = deepcopy(first)
    wrong_redactor["redactor_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="redactor identity"):
        validate_dataset_manifest_contract(wrong_redactor, rows, items, _provenance())

    wrong_exclusions = deepcopy(first)
    wrong_exclusions["exclusions"]["count"] += 1
    with pytest.raises(ValueError, match="exclusions"):
        validate_dataset_manifest_contract(wrong_exclusions, rows, items, _provenance())

    wrong_source = deepcopy(first)
    wrong_source["source_artifacts"]["raw_trajectories_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="source artifact identity"):
        validate_dataset_manifest_contract(wrong_source, rows, items, _provenance())

    wrong_counts = deepcopy(first)
    wrong_counts["counts"]["eligible"] = 49
    wrong_counts["counts"]["not_selected"] = 29
    wrong_counts["exclusions"] = {
        "count": 30,
        "by_reason": {
            "ineligible": 1,
            "not_selected_fixed_slice_capacity": 29,
            "quarantined": 0,
        },
    }
    with pytest.raises(ValueError, match="counts"):
        validate_dataset_manifest_contract(wrong_counts, rows, items, _provenance())


def test_replay_fixture_uses_the_same_pseudonyms_as_redacted_inputs() -> None:
    catalog = load_eval_catalog(LAB_ROOT / "config" / "eval")

    state = pseudonymized_fixture_state(catalog, "campaigns-v1")

    assert [row["id"] for row in state["campaigns"]] == [
        "campaign_001",
        "campaign_002",
        "campaign_003",
    ]
    assert "camp_" not in json.dumps(state)


def test_environment_behavior_negative_is_not_eligible_for_dataset(tmp_path: Path) -> None:
    catalog = load_eval_catalog(LAB_ROOT / "config" / "eval")
    items = _normalized()
    environment = next(item for item in items if item["scenario_kind"] == "environment")
    environment["attribution"]["dataset_eligibility"] = "behavior_negative"
    sample, _ = create_review_material(items, "c" * 64)
    review = _approved_review(items, sample)

    with pytest.raises(ValueError, match="environment behavior-negative"):
        build_dataset(
            items,
            catalog,
            sample,
            review,
            output_dir=tmp_path,
            source_date_epoch=1_700_000_000,
            metadata={},
            provenance=_provenance(),
        )
