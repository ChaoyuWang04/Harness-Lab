from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from app.eval.classify import classify_trajectory


REDACTOR_VERSION = "m4-redactor-v1"
CAMPAIGN_PATTERN = re.compile(r"\bcamp_\d+\b")
SECRET_PATTERNS = (
    re.compile(r"(?i)(?:authorization\s*:\s*)?bearer\s+[^\s,;]+"),
    re.compile(r"(?i)(?:postgresql|postgres|redis)://[^\s\"']+"),
    re.compile(r"(?i)https?://[^\s\"']*@[^\s\"']+"),
    re.compile(r"(?i)(?:api[_-]?key|secret|token)\s*[:=]\s*[^\s,;]+"),
)
REQUIRED_RAW_FIELDS = frozenset(
    {
        "run_id",
        "case_id",
        "scenario_kind",
        "world_fixture_id",
        "fault_schedule_id",
        "status",
        "events",
        "model_turns",
        "retry_statuses",
        "audits",
        "source_sha256",
    }
)


def _campaign_map(trajectories: list[dict[str, Any]]) -> dict[str, str]:
    found: set[str] = set()
    for trajectory in trajectories:
        found.update(CAMPAIGN_PATTERN.findall(str(trajectory)))
    return {
        campaign_id: f"campaign_{index:03d}"
        for index, campaign_id in enumerate(sorted(found), start=1)
    }


def _sanitize_text(value: str, pseudonyms: dict[str, str]) -> str:
    for original, replacement in pseudonyms.items():
        value = value.replace(original, replacement)
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def _sanitize(value: Any, pseudonyms: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value, pseudonyms)
    if isinstance(value, list):
        return [_sanitize(item, pseudonyms) for item in value]
    if isinstance(value, dict):
        return {
            key: _sanitize(item, pseudonyms)
            for key, item in value.items()
            if key.lower()
            not in {
                "authorization",
                "cookie",
                "set-cookie",
                "x-api-key",
                "dsn",
                "database_url",
                "redis_url",
                "upstream_url",
            }
        }
    return value


def scan_secret_hits(value: Any) -> list[str]:
    rendered = str(value)
    hits: list[str] = []
    for index, pattern in enumerate(SECRET_PATTERNS):
        if pattern.search(rendered):
            hits.append(f"pattern-{index}")
    for forbidden in ("authorization", "cookie", "dsn", "database_url", "redis_url"):
        if forbidden in rendered.lower():
            hits.append(f"field-{forbidden}")
    return sorted(set(hits))


def _messages(raw: dict[str, Any], pseudonyms: dict[str, str]) -> list[dict[str, Any]]:
    turns = raw["model_turns"]
    last_input = turns[-1].get("input_messages", []) if turns else []
    messages: list[dict[str, Any]] = []
    for message in last_input:
        normalized = {
            "role": message.get("role", "user"),
            "content": _sanitize(message.get("content"), pseudonyms),
        }
        if message.get("tool_call_id") is not None:
            normalized["tool_call_id"] = message["tool_call_id"]
        messages.append(normalized)
    for turn in turns:
        run_attempt, step, model_attempt = turn["identity"]
        if turn.get("error_code"):
            messages.append(
                {
                    "role": "model_error",
                    "content": turn["error_code"],
                    "step": step,
                    "model_attempt": model_attempt,
                }
            )
    final_turn = turns[-1]
    if final_turn.get("output_content") is not None or final_turn.get("tool_calls"):
        messages.append(
            {
                "role": "assistant",
                "content": _sanitize(
                    {
                        "text": final_turn.get("output_content"),
                        "tool_calls": final_turn.get("tool_calls", []),
                    },
                    pseudonyms,
                ),
                "step": final_turn["identity"][1],
                "model_attempt": final_turn["identity"][2],
            }
        )
    return messages


def normalize_trajectories(trajectories: list[dict[str, Any]]) -> dict[str, Any]:
    raw_copy = deepcopy(trajectories)
    pseudonyms = _campaign_map(raw_copy)
    normalized: list[dict[str, Any]] = []
    quarantine: list[dict[str, str]] = []
    for raw in raw_copy:
        missing = REQUIRED_RAW_FIELDS - raw.keys()
        if missing or not raw.get("model_turns"):
            quarantine.append(
                {"run_id": str(raw.get("run_id", "unknown")), "reason_code": "MISSING_REQUIRED_EVIDENCE"}
            )
            continue
        if raw.get("lineage_conflict"):
            quarantine.append(
                {"run_id": str(raw["run_id"]), "reason_code": "LINEAGE_CONFLICT"}
            )
            continue
        item = {
            "schema_version": "trajectory_v1",
            "processor_version": REDACTOR_VERSION,
            "run_id": raw["run_id"],
            "case_id": raw["case_id"],
            "scenario_kind": raw["scenario_kind"],
            "messages": _messages(raw, pseudonyms),
            "terminal": {
                "status": raw["status"],
                "error_code": raw.get("error_code"),
            },
            "attribution": {
                key: value
                for key, value in classify_trajectory(raw).items()
                if key != "reason_codes"
            },
            "lineage": {
                "source_sha256": raw["source_sha256"],
                "world_fixture_id": raw["world_fixture_id"],
                "fault_schedule_id": raw["fault_schedule_id"],
            },
        }
        if scan_secret_hits(item):
            quarantine.append(
                {"run_id": str(raw["run_id"]), "reason_code": "SECRET_REDACTION_FAILED"}
            )
            continue
        normalized.append(item)
    normalized.sort(key=lambda item: (item["case_id"], item["run_id"]))
    quarantine.sort(key=lambda item: (item["run_id"], item["reason_code"]))
    return {
        "processor_version": REDACTOR_VERSION,
        "pseudonym_map": pseudonyms,
        "normalized": normalized,
        "quarantine": quarantine,
    }
