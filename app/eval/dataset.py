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

from app.eval.artifacts import atomic_write_json, atomic_write_jsonl, canonical_json_bytes, sha256_file
from app.eval.catalog import EvalCatalog, canonical_json, sha256_bytes
from app.eval.classify import CLASSIFIER_VERSION, classifier_sha256
from app.eval.redact import REDACTOR_VERSION, redactor_sha256


class HumanReviewError(RuntimeError):
    pass


PROVENANCE_SHA_FIELDS = (
    "raw_trajectories_sha256",
    "raw_export_manifest_sha256",
    "normalized_trajectories_sha256",
    "normalized_manifest_sha256",
    "quarantine_manifest_sha256",
)


def dataset_provenance_from_artifacts(artifact_dir: Path) -> dict[str, Any]:
    raw_path = artifact_dir / "raw" / "trajectories.jsonl"
    raw_manifest_path = artifact_dir / "raw" / "export_manifest.json"
    normalized_path = artifact_dir / "normalized" / "trajectories.jsonl"
    normalized_manifest_path = artifact_dir / "normalized" / "manifest.json"
    quarantine_path = artifact_dir / "quarantine" / "trajectories.jsonl"
    quarantine_manifest_path = artifact_dir / "quarantine" / "exclusions.json"
    raw = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    normalized = [
        json.loads(line)
        for line in normalized_path.read_text(encoding="utf-8").splitlines()
    ]
    raw_manifest = json.loads(raw_manifest_path.read_text(encoding="utf-8"))
    normalized_manifest = json.loads(normalized_manifest_path.read_text(encoding="utf-8"))
    quarantine = [
        json.loads(line)
        for line in quarantine_path.read_text(encoding="utf-8").splitlines()
    ]
    quarantine_manifest = json.loads(quarantine_manifest_path.read_text(encoding="utf-8"))
    raw_sha = sha256_file(raw_path)
    normalized_sha = sha256_file(normalized_path)
    if raw_manifest.get("trajectories_sha256") != raw_sha:
        raise ValueError("raw export manifest SHA mismatch")
    if raw_manifest.get("count") != len(raw):
        raise ValueError("raw export manifest count mismatch")
    if normalized_manifest.get("sha256") != normalized_sha:
        raise ValueError("normalized manifest SHA mismatch")
    if normalized_manifest.get("raw_sha256") != raw_sha:
        raise ValueError("normalized manifest raw SHA mismatch")
    if normalized_manifest.get("count") != len(normalized):
        raise ValueError("normalized manifest count mismatch")
    if quarantine_manifest.get("count") != len(quarantine):
        raise ValueError("quarantine manifest count mismatch")
    prompt_versions = {
        turn.get("prompt_version")
        for item in raw
        for turn in item.get("model_turns", [])
    }
    if len(prompt_versions) != 1 or not all(
        isinstance(version, str) and version for version in prompt_versions
    ):
        raise ValueError("source trajectories require exactly one prompt_version")
    return {
        "prompt_version": next(iter(prompt_versions)),
        "raw_trajectories_sha256": raw_sha,
        "raw_export_manifest_sha256": sha256_file(raw_manifest_path),
        "normalized_trajectories_sha256": normalized_sha,
        "normalized_manifest_sha256": sha256_file(normalized_manifest_path),
        "quarantine_manifest_sha256": sha256_file(quarantine_manifest_path),
        "quarantine_count": len(quarantine),
    }


def validate_dataset_manifest_contract(
    manifest: dict[str, Any],
    rows: list[dict[str, Any]],
    normalized: list[dict[str, Any]],
    provenance: dict[str, Any],
) -> dict[str, int]:
    sha_pattern = re.compile(r"[0-9a-f]{64}")
    prompt = manifest.get("prompt")
    if not isinstance(prompt, dict) or not isinstance(prompt.get("version"), str) or not sha_pattern.fullmatch(
        str(prompt.get("sha256", ""))
    ):
        raise ValueError("dataset manifest prompt provenance is incomplete")
    schemas = manifest.get("schemas")
    if not isinstance(schemas, dict) or set(schemas) != {"dataset", "trajectory"}:
        raise ValueError("dataset manifest schema provenance is incomplete")
    config_root = Path(__file__).resolve().parents[2] / "config" / "eval"
    expected_schemas = {
        "dataset": ("dataset_v1", config_root / "dataset_v1.schema.json"),
        "trajectory": ("trajectory_v1", config_root / "trajectory_v1.schema.json"),
    }
    for name, (version, path) in expected_schemas.items():
        if schemas[name] != {"version": version, "sha256": sha256_file(path)}:
            raise ValueError(f"dataset manifest {name} schema identity mismatch")
    if (
        manifest.get("classifier_version") != CLASSIFIER_VERSION
        or manifest.get("classifier_sha256") != classifier_sha256()
    ):
        raise ValueError("dataset manifest classifier identity mismatch")
    if (
        manifest.get("redactor_version") != REDACTOR_VERSION
        or manifest.get("redactor_sha256") != redactor_sha256()
    ):
        raise ValueError("dataset manifest redactor identity mismatch")
    system_prompts = {
        message["content"]
        for item in normalized
        for message in item.get("messages", [])
        if message.get("role") == "system" and isinstance(message.get("content"), str)
    }
    expected_prompt_sha = (
        hashlib.sha256(next(iter(system_prompts)).encode("utf-8")).hexdigest()
        if len(system_prompts) == 1
        else None
    )
    if expected_prompt_sha is None or prompt["sha256"] != expected_prompt_sha:
        raise ValueError("dataset manifest prompt identity mismatch")
    if prompt["version"] != provenance.get("prompt_version"):
        raise ValueError("dataset manifest prompt version identity mismatch")
    source_artifacts = manifest.get("source_artifacts")
    if not isinstance(source_artifacts, dict) or set(source_artifacts) != set(
        PROVENANCE_SHA_FIELDS
    ) or not all(sha_pattern.fullmatch(str(value)) for value in source_artifacts.values()):
        raise ValueError("dataset manifest source artifact provenance is incomplete")
    expected_source_artifacts = {
        field: provenance.get(field) for field in PROVENANCE_SHA_FIELDS
    }
    if source_artifacts != expected_source_artifacts:
        raise ValueError("dataset manifest source artifact identity mismatch")
    counts = manifest.get("counts")
    expected_selected = len(rows)
    expected_eligible = sum(
        item.get("attribution", {}).get("dataset_eligibility") != "quarantine"
        for item in normalized
    )
    expected_quarantined = provenance.get("quarantine_count")
    if not isinstance(counts, dict) or set(counts) != {
        "normalized",
        "eligible",
        "selected",
        "not_selected",
        "quarantined",
    }:
        raise ValueError("dataset manifest counts are incomplete")
    if (
        counts["selected"] != expected_selected
        or counts["normalized"] != len(normalized)
        or counts["eligible"] != expected_eligible
        or counts["quarantined"] != expected_quarantined
        or counts["eligible"] - counts["selected"] != counts["not_selected"]
        or counts["normalized"] + counts["quarantined"] != 50
        or counts["normalized"] < counts["eligible"]
    ):
        raise ValueError("dataset manifest counts are inconsistent")
    exclusions = manifest.get("exclusions")
    expected_by_reason = {
        "ineligible": counts["normalized"] - counts["eligible"],
        "not_selected_fixed_slice_capacity": counts["not_selected"],
        "quarantined": counts["quarantined"],
    }
    if not isinstance(exclusions, dict) or exclusions != {
        "count": sum(expected_by_reason.values()),
        "by_reason": expected_by_reason,
    }:
        raise ValueError("dataset manifest exclusions are incomplete or inconsistent")
    structured = sum(
        1
        for row in rows
        if isinstance(row.get("expected_behavior"), dict)
        and row["expected_behavior"].get("assertions") == row.get("assertions")
        and isinstance(row["expected_behavior"].get("label"), str)
    )
    if structured != expected_selected:
        raise ValueError("dataset expected_behavior is not executable and structured")
    dataset_sha = hashlib.sha256(b"".join(canonical_json_bytes(row) for row in rows)).hexdigest()
    if manifest.get("dataset_sha256") != dataset_sha or manifest.get("count") != expected_selected:
        raise ValueError("dataset manifest dataset identity mismatch")
    if not isinstance(manifest.get("metadata"), dict) or not {
        "commit",
        "model",
    } <= manifest["metadata"].keys():
        raise ValueError("dataset manifest build metadata is incomplete")
    return {
        "dataset_items": expected_selected,
        "excluded_items": exclusions["count"],
        "schema_files": len(schemas),
        "source_artifacts": len(source_artifacts),
        "structured_expected_behavior": structured,
    }


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
    assertions = [assertion.model_dump(mode="json") for assertion in case.expected_behavior.assertions]
    operators = {assertion["operator"] for assertion in assertions}
    if "sse_sequence_continuous" not in operators:
        assertions.append({"operator": "sse_sequence_continuous", "expected": True})
    if "expected_post_state" not in operators:
        assertions.append({"operator": "expected_post_state", "expected": expected_post_state})
    return {
        "id": f"m4-{case.case_id}",
        "input": input_text,
        "expected_behavior": {
            "label": f"{case.scenario_kind}:{item['attribution']['behavior_label']}",
            "assertions": deepcopy(assertions),
        },
        "assertions": assertions,
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
    provenance: dict[str, Any],
) -> dict[str, Any]:
    review_result = validate_human_review(normalized, sample, review)
    if any(
        item["scenario_kind"] == "environment"
        and item["attribution"]["dataset_eligibility"] == "behavior_negative"
        for item in normalized
    ):
        raise ValueError("environment behavior-negative trajectories are not dataset eligible")
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

    if not isinstance(provenance.get("prompt_version"), str) or not provenance["prompt_version"]:
        raise ValueError("dataset provenance requires prompt_version")
    for field in PROVENANCE_SHA_FIELDS:
        value = provenance.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"dataset provenance requires SHA-256 field: {field}")
    quarantine_count = provenance.get("quarantine_count")
    if not isinstance(quarantine_count, int) or quarantine_count < 0:
        raise ValueError("dataset provenance requires non-negative quarantine_count")
    system_prompts = {
        message["content"]
        for item in normalized
        for message in item["messages"]
        if message.get("role") == "system" and isinstance(message.get("content"), str)
    }
    if len(system_prompts) != 1:
        raise ValueError("dataset requires exactly one system prompt")
    prompt_sha256 = hashlib.sha256(next(iter(system_prompts)).encode("utf-8")).hexdigest()

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
        "schemas": {
            "dataset": {
                "version": "dataset_v1",
                "sha256": sha256_file(schema_path),
            },
            "trajectory": {
                "version": "trajectory_v1",
                "sha256": sha256_file(schema_path.with_name("trajectory_v1.schema.json")),
            },
        },
        "prompt": {
            "version": provenance["prompt_version"],
            "sha256": prompt_sha256,
        },
        "source_artifacts": {
            field: provenance[field] for field in PROVENANCE_SHA_FIELDS
        },
        "counts": {
            "normalized": len(normalized),
            "eligible": len(eligible),
            "selected": len(rows),
            "not_selected": len(eligible) - len(rows),
            "quarantined": quarantine_count,
        },
        "classifier_version": CLASSIFIER_VERSION,
        "classifier_sha256": classifier_sha256(),
        "redactor_version": REDACTOR_VERSION,
        "redactor_sha256": redactor_sha256(),
        "exclusions": {
            "count": len(normalized) - len(rows) + quarantine_count,
            "by_reason": {
                "ineligible": len(normalized) - len(eligible),
                "not_selected_fixed_slice_capacity": len(eligible) - len(rows),
                "quarantined": quarantine_count,
            },
        },
        "review": review_result,
        "review_sample_sha256": sample["sample_sha256"],
        "metadata": metadata,
    }
    validate_dataset_manifest_contract(manifest, rows, normalized, provenance)
    atomic_write_json(output_dir / "dataset_v1.manifest.json", manifest)
    return manifest
