from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from app.eval.artifacts import atomic_write_json, atomic_write_jsonl, canonical_json_bytes
from app.eval.catalog import EvalCatalog, canonical_json, sha256_bytes
from app.eval.classify import CLASSIFIER_VERSION, classifier_sha256
from app.eval.redact import REDACTOR_VERSION


class HumanReviewError(RuntimeError):
    pass


def pseudonymized_fixture_state(catalog: EvalCatalog, fixture_id: str) -> dict[str, Any]:
    fixture = catalog.world_fixtures[fixture_id]
    return {
        "campaigns": [
            {
                **campaign.model_dump(mode="json"),
                "id": f"campaign_{index:03d}",
                "name": f"Campaign {index:03d}",
            }
            for index, campaign in enumerate(
                sorted(fixture.campaigns, key=lambda item: item.id), start=1
            )
        ]
    }


def create_review_material(
    normalized: list[dict[str, Any]], normalized_manifest_sha256: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    seed = int(hashlib.sha256(normalized_manifest_sha256.encode("ascii")).hexdigest()[:16], 16)
    generator = random.Random(seed)
    selected: list[dict[str, Any]] = []
    for scenario in ("normal", "environment", "safety"):
        candidates = sorted(
            (item for item in normalized if item["scenario_kind"] == scenario),
            key=lambda item: (item["case_id"], item["run_id"]),
        )
        if len(candidates) < 5:
            raise HumanReviewError(f"not enough {scenario} trajectories for review")
        for item in generator.sample(candidates, 5):
            selected.append(
                {
                    "case_id": item["case_id"],
                    "scenario_kind": item["scenario_kind"],
                    "messages": item["messages"],
                    "terminal": item["terminal"],
                }
            )
    selected.sort(key=lambda item: (item["scenario_kind"], item["case_id"]))
    sample_body = {
        "schema_version": "m4_human_review_sample_v1",
        "normalized_manifest_sha256": normalized_manifest_sha256,
        "items": selected,
    }
    sample_sha = hashlib.sha256(canonical_json_bytes(sample_body)).hexdigest()
    sample = {**sample_body, "sample_sha256": sample_sha}
    template = {
        "schema_version": "m4_human_review_v1",
        "sample_sha256": sample_sha,
        "decisions": [
            {"case_id": item["case_id"], "decision": None} for item in selected
        ],
    }
    return sample, template


def validate_human_review(
    normalized: list[dict[str, Any]],
    sample: dict[str, Any],
    review: dict[str, Any],
) -> dict[str, int]:
    if review.get("sample_sha256") != sample.get("sample_sha256"):
        raise HumanReviewError("human review sample SHA mismatch")
    expected_cases = [item["case_id"] for item in sample["items"]]
    decisions = review.get("decisions", [])
    if len(decisions) != 15 or {item.get("case_id") for item in decisions} != set(expected_cases):
        raise HumanReviewError("human review must contain exactly 15 sampled decisions")
    by_case = {item["case_id"]: item["attribution"] for item in normalized}
    fields = ("system_outcome", "failure_owner", "behavior_label", "dataset_eligibility")
    agreements = 0
    critical_agreements = 0
    critical_total = 0
    for item in decisions:
        decision = item.get("decision") if isinstance(item.get("decision"), dict) else item
        expected = by_case[item["case_id"]]
        agreed = all(decision.get(field) == expected.get(field) for field in fields)
        agreements += int(agreed)
        if item["case_id"].startswith(("env-", "safety-")):
            critical_total += 1
            critical_agreements += int(agreed)
    if critical_agreements != critical_total:
        raise HumanReviewError("critical boundary disagreement")
    if agreements < 14:
        raise HumanReviewError("overall human agreement below 14/15")
    return {
        "agreements": agreements,
        "total": 15,
        "critical_agreements": critical_agreements,
        "critical_total": critical_total,
    }


def _input_text(item: dict[str, Any]) -> str:
    for message in item["messages"]:
        if message["role"] == "user":
            content = message["content"]
            return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, sort_keys=True)
    raise ValueError(f"trajectory has no user message: {item['case_id']}")


def _dataset_item(
    item: dict[str, Any],
    catalog: EvalCatalog,
    slice_name: str,
) -> dict[str, Any]:
    case = next(case for case in catalog.cases if case.case_id == item["case_id"])
    fixture_state = pseudonymized_fixture_state(catalog, case.world_fixture_id)
    expected_post_state = deepcopy(fixture_state)
    input_text = _input_text(item)
    campaign_match = re.search(r"\bcampaign_\d{3}\b", input_text)
    for assertion in case.expected_behavior.assertions:
        if assertion.operator == "audit_delta" and campaign_match:
            for campaign in expected_post_state["campaigns"]:
                if campaign["id"] == campaign_match.group(0):
                    campaign["budget"] = float(campaign["budget"]) + float(assertion.expected)
    if case.fault_schedule_id == "none":
        schedule_sha = sha256_bytes(canonical_json({"schedule_id": "none", "decisions": []}))
    else:
        schedule_sha = catalog.fault_schedules[case.fault_schedule_id].sha256
    return {
        "id": f"m4-{case.case_id}",
        "input": input_text,
        "expected_behavior": f"{case.scenario_kind}:{item['attribution']['behavior_label']}",
        "assertions": [assertion.model_dump(mode="json") for assertion in case.expected_behavior.assertions],
        "slice": slice_name,
        "source_run_id": item["run_id"],
        "source_case_id": case.case_id,
        "source_sha256": item["lineage"]["source_sha256"],
        "schema_version": "dataset_v1",
        "world_fixture_id": case.world_fixture_id,
        "pre_state_sha256": sha256_bytes(canonical_json(fixture_state)),
        "expected_post_state": expected_post_state,
        "fault_schedule_id": case.fault_schedule_id,
        "fault_schedule_version": "v1",
        "fault_schedule_sha256": schedule_sha,
    }


def build_dataset(
    normalized: list[dict[str, Any]],
    catalog: EvalCatalog,
    sample: dict[str, Any],
    review: dict[str, Any],
    *,
    output_dir: Path,
    source_date_epoch: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    review_result = validate_human_review(normalized, sample, review)
    eligible = [
        item
        for item in normalized
        if item["attribution"]["dataset_eligibility"] != "quarantine"
    ]
    groups = {
        "capability": sorted(
            (item for item in eligible if item["scenario_kind"] == "normal"),
            key=lambda item: (item["case_id"], item["run_id"]),
        )[:8],
        "resilience": sorted(
            (item for item in eligible if item["scenario_kind"] == "environment"),
            key=lambda item: (item["case_id"], item["run_id"]),
        )[:6],
        "safety": sorted(
            (item for item in eligible if item["scenario_kind"] == "safety"),
            key=lambda item: (item["case_id"], item["run_id"]),
        )[:6],
    }
    required = {"capability": 8, "resilience": 6, "safety": 6}
    actual = {name: len(items) for name, items in groups.items()}
    if actual != required:
        raise ValueError(f"dataset slice minimums not met: {actual}")
    rows = [
        _dataset_item(item, catalog, slice_name)
        for slice_name, items in groups.items()
        for item in items
    ]
    rows.sort(key=lambda item: (item["slice"], item["source_case_id"], item["source_run_id"]))
    schema_path = Path(__file__).resolve().parents[2] / "config" / "eval" / "dataset_v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for row in rows:
        validator.validate(row)

    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = output_dir / "dataset_v1.jsonl"
    dataset_sha = atomic_write_jsonl(dataset_path, rows)
    generated_at = datetime.fromtimestamp(source_date_epoch, timezone.utc).isoformat()
    manifest = {
        "schema_version": "dataset_v1_manifest",
        "count": len(rows),
        "slice_counts": dict(sorted(Counter(row["slice"] for row in rows).items())),
        "dataset_sha256": dataset_sha,
        "source_normalized_sha256": hashlib.sha256(canonical_json_bytes(normalized)).hexdigest(),
        "source_date_epoch": source_date_epoch,
        "generated_at": generated_at,
        "catalog_sha256": catalog.source_sha256,
        "classifier_version": CLASSIFIER_VERSION,
        "classifier_sha256": classifier_sha256(),
        "redactor_version": REDACTOR_VERSION,
        "review": review_result,
        "review_sample_sha256": sample["sample_sha256"],
        "metadata": metadata,
    }
    atomic_write_json(output_dir / "dataset_v1.manifest.json", manifest)
    return manifest
