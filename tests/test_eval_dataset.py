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
        "messages": [{"role": "user", "content": f"prompt {case_id}"}],
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
    )
    second = build_dataset(
        deepcopy(items),
        catalog,
        deepcopy(sample),
        deepcopy(review),
        output_dir=tmp_path / "second",
        source_date_epoch=1_700_000_000,
        metadata={"commit": "a" * 40, "model": "test-model"},
    )

    assert first == second
    assert first["count"] >= 20
    assert first["slice_counts"] == {"capability": 8, "resilience": 6, "safety": 6}
    assert (tmp_path / "first" / "dataset_v1.jsonl").read_bytes() == (
        tmp_path / "second" / "dataset_v1.jsonl"
    ).read_bytes()
    assert (tmp_path / "first" / "dataset_v1.manifest.json").read_bytes() == (
        tmp_path / "second" / "dataset_v1.manifest.json"
    ).read_bytes()
    rows = [json.loads(line) for line in (tmp_path / "first" / "dataset_v1.jsonl").read_text().splitlines()]
    assert len({row["id"] for row in rows}) == len(rows)
    assert all(row["assertions"] for row in rows)
    assert all(len(row["source_sha256"]) == 64 for row in rows)


def test_replay_fixture_uses_the_same_pseudonyms_as_redacted_inputs() -> None:
    catalog = load_eval_catalog(LAB_ROOT / "config" / "eval")

    state = pseudonymized_fixture_state(catalog, "campaigns-v1")

    assert [row["id"] for row in state["campaigns"]] == [
        "campaign_001",
        "campaign_002",
        "campaign_003",
    ]
    assert "camp_" not in json.dumps(state)
