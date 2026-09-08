from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.eval.artifacts import canonical_json_bytes  # noqa: E402
from app.eval.redact import REDACTOR_VERSION, normalize_trajectories, scan_secret_hits  # noqa: E402


def _raw(run_id: str, campaign_id: str) -> dict[str, object]:
    return {
        "schema_version": "raw_trajectory_v1",
        "run_id": run_id,
        "case_id": "normal-01",
        "scenario_kind": "normal",
        "world_fixture_id": "campaigns-v1",
        "fault_schedule_id": "none",
        "prompt": f"查询 {campaign_id}",
        "status": "completed",
        "result": {"answer": f"{campaign_id} 正常"},
        "error_code": None,
        "events": [{"sequence": 1, "type": "run.completed", "payload": {}}],
        "model_turns": [
            {
                "identity": [1, 1, 0],
                "prompt_version": "v1",
                "input_messages": [
                    {"role": "user", "content": f"查询 {campaign_id}; Authorization: Bearer fake-secret"}
                ],
                "output_content": f"{campaign_id} 正常，postgresql://u:p@db:5432/harness",
                "tool_calls": [],
                "usage": {"total": 4},
                "error_code": None,
            }
        ],
        "retry_statuses": [],
        "audits": [],
        "source_sha256": "a" * 64,
        "authorization": "Bearer fake-secret",
        "cookie": "session=fake-cookie",
        "redis_url": "redis://redis:6379/0",
        "dsn": "https://fake@sentry.invalid/1",
    }


def test_normalization_uses_stable_dataset_wide_campaign_pseudonyms() -> None:
    raw = [_raw("run-1", "camp_002"), _raw("run-2", "camp_001")]
    before = canonical_json_bytes(raw)

    result = normalize_trajectories(raw)

    assert canonical_json_bytes(raw) == before
    assert result["quarantine"] == []
    serialized = canonical_json_bytes(result["normalized"]).decode("utf-8")
    assert "camp_001" not in serialized
    assert "camp_002" not in serialized
    assert "campaign_001" in serialized
    assert "campaign_002" in serialized
    assert result["pseudonym_map"] == {
        "camp_001": "campaign_001",
        "camp_002": "campaign_002",
    }


def test_normalization_removes_fake_secrets_connections_and_unowned_fields() -> None:
    result = normalize_trajectories([_raw("run-1", "camp_001")])
    [normalized] = result["normalized"]

    assert scan_secret_hits(normalized) == []
    serialized = canonical_json_bytes(normalized).decode("utf-8").lower()
    for forbidden in (
        "fake-secret",
        "fake-cookie",
        "postgresql://",
        "redis://",
        "sentry.invalid",
        "authorization",
        "cookie",
        "dsn",
    ):
        assert forbidden not in serialized
    assert normalized["processor_version"] == REDACTOR_VERSION


def test_normalized_output_validates_against_trajectory_schema() -> None:
    schema = json.loads(
        (LAB_ROOT / "config" / "eval" / "trajectory_v1.schema.json").read_text(encoding="utf-8")
    )
    [normalized] = normalize_trajectories([_raw("run-1", "camp_001")])["normalized"]

    Draft202012Validator(schema).validate(normalized)


def test_missing_required_evidence_lineage_conflict_and_invalid_schema_are_quarantined() -> None:
    missing = _raw("run-1", "camp_001")
    missing.pop("model_turns")
    conflict = deepcopy(_raw("run-2", "camp_002"))
    conflict["lineage_conflict"] = True
    invalid = deepcopy(_raw("run-3", "camp_003"))
    invalid["scenario_kind"] = "unknown"

    result = normalize_trajectories([missing, conflict, invalid])

    assert result["normalized"] == []
    assert [item["reason_code"] for item in result["quarantine"]] == [
        "MISSING_REQUIRED_EVIDENCE",
        "LINEAGE_CONFLICT",
        "INVALID_NORMALIZED_SCHEMA",
    ]


def test_normalization_is_byte_deterministic() -> None:
    raw = [_raw("run-1", "camp_001")]
    assert canonical_json_bytes(normalize_trajectories(raw)) == canonical_json_bytes(
        normalize_trajectories(deepcopy(raw))
    )
