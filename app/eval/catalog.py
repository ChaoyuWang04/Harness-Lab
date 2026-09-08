from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


FaultStatus = Literal["200", "429", "timeout", "503"]
ScenarioKind = Literal["normal", "environment", "safety"]


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedAssertion(StrictModel):
    operator: str = Field(min_length=1)
    expected: Any


class ExpectedBehavior(StrictModel):
    assertions: list[ExpectedAssertion] = Field(min_length=1)


class EvalCase(StrictModel):
    case_id: str = Field(pattern=r"^(normal|env|safety)-\d{2}$")
    prompt: str = Field(min_length=1)
    scenario_kind: ScenarioKind
    world_fixture_id: str = Field(min_length=1)
    expected_terminal_class: Literal["terminal", "completed", "failed"]
    expected_behavior: ExpectedBehavior
    fault_schedule_id: str


class CampaignFixture(StrictModel):
    id: str
    name: str
    budget: float
    spend_today: float
    status: str


class WorldFixture(StrictModel):
    fixture_id: str
    campaigns: list[CampaignFixture] = Field(min_length=1)

    @property
    def pre_state_sha256(self) -> str:
        ordered = sorted(
            (campaign.model_dump(mode="json") for campaign in self.campaigns),
            key=lambda item: item["id"],
        )
        return sha256_bytes(canonical_json({"campaigns": ordered}))


class FaultScheduleSpec(StrictModel):
    schedule_id: str = Field(pattern=r"^env-\d{2}$")
    decisions: list[FaultStatus] = Field(min_length=1, max_length=4)

    @property
    def sha256(self) -> str:
        return sha256_bytes(canonical_json(self.model_dump(mode="json")))


class EvalCatalog(StrictModel):
    cases: list[EvalCase]
    world_fixtures: dict[str, WorldFixture]
    fault_schedules: dict[str, FaultScheduleSpec]
    source_sha256: dict[str, str]

    @model_validator(mode="after")
    def validate_references_and_frozen_shape(self) -> "EvalCatalog":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("duplicate case_id")

        for key, fixture in self.world_fixtures.items():
            if key != fixture.fixture_id:
                raise ValueError("world fixture key does not match fixture_id")
        for key, schedule in self.fault_schedules.items():
            if key != schedule.schedule_id:
                raise ValueError("fault schedule key does not match schedule_id")

        for case in self.cases:
            if case.world_fixture_id not in self.world_fixtures:
                raise ValueError(f"unknown world fixture: {case.world_fixture_id}")
            if case.fault_schedule_id != "none" and case.fault_schedule_id not in self.fault_schedules:
                raise ValueError(f"unknown fault schedule: {case.fault_schedule_id}")
            if case.scenario_kind != "environment" and case.fault_schedule_id != "none":
                raise ValueError("normal and safety cases must use fault schedule none")
            if case.scenario_kind == "environment" and case.fault_schedule_id != case.case_id:
                raise ValueError("environment case must use matching fault schedule")

        expected_ids = {f"env-{index:02d}" for index in range(1, 11)}
        if set(self.fault_schedules) != expected_ids:
            raise ValueError("fault schedule IDs differ from the frozen M4 set")
        expected_decisions: dict[str, list[str]] = {
            "env-01": ["429", "429", "200"],
            "env-02": ["timeout", "timeout", "200"],
            "env-03": ["503", "503", "200"],
            "env-04": ["429", "503", "200"],
            "env-05": ["timeout", "503", "200"],
            "env-06": ["429", "429", "429", "429"],
            "env-07": ["timeout", "timeout", "timeout", "timeout"],
            "env-08": ["503", "503", "503", "503"],
            "env-09": ["429", "timeout", "503", "429"],
            "env-10": ["timeout", "503", "429", "timeout"],
        }
        actual = {key: schedule.decisions for key, schedule in self.fault_schedules.items()}
        if actual != expected_decisions:
            raise ValueError("fault decisions differ from the frozen M4 sequences")
        return self

    def environment_attempt_totals(self) -> dict[str, int]:
        counts = Counter(
            decision
            for case in self.cases
            if case.scenario_kind == "environment"
            for decision in self.fault_schedules[case.fault_schedule_id].decisions
        )
        return {key: counts[key] for key in ("200", "429", "timeout", "503")}


def _read_yaml(path: Path) -> tuple[Any, str]:
    raw = path.read_bytes()
    return yaml.safe_load(raw), sha256_bytes(raw)


def load_eval_catalog(config_root: Path) -> EvalCatalog:
    paths = {
        "cohort_v1.yaml": config_root / "cohort_v1.yaml",
        "world_fixtures_v1.yaml": config_root / "world_fixtures_v1.yaml",
        "fault_schedules_v1.yaml": config_root / "fault_schedules_v1.yaml",
    }
    loaded = {name: _read_yaml(path) for name, path in paths.items()}
    cohort = loaded["cohort_v1.yaml"][0]
    fixtures = loaded["world_fixtures_v1.yaml"][0]
    schedules = loaded["fault_schedules_v1.yaml"][0]
    return EvalCatalog.model_validate(
        {
            "cases": cohort["cases"],
            "world_fixtures": {
                item["fixture_id"]: item for item in fixtures["world_fixtures"]
            },
            "fault_schedules": {
                item["schedule_id"]: item for item in schedules["fault_schedules"]
            },
            "source_sha256": {name: digest for name, (_, digest) in loaded.items()},
        }
    )
