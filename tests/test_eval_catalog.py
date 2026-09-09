from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.eval.catalog import EvalCatalog, load_eval_catalog


CONFIG_ROOT = LAB_ROOT / "config" / "eval"


def test_catalog_freezes_exact_m4_cohort_and_fault_totals() -> None:
    catalog = load_eval_catalog(CONFIG_ROOT)

    assert len(catalog.cases) == 50
    assert Counter(case.scenario_kind for case in catalog.cases) == {
        "normal": 30,
        "environment": 10,
        "safety": 10,
    }
    assert catalog.environment_attempt_totals() == {
        "200": 5,
        "429": 10,
        "timeout": 10,
        "503": 10,
    }
    assert len({case.case_id for case in catalog.cases}) == 50
    assert all(case.expected_behavior.assertions for case in catalog.cases)
    recovered_answer_facts = {
        case.case_id: next(
            assertion.expected
            for assertion in case.expected_behavior.assertions
            if assertion.operator == "answer_fact"
        )
        for case in catalog.cases
        if case.case_id in {"env-01", "env-02", "env-03", "env-04", "env-05"}
    }
    assert recovered_answer_facts == {
        "env-01": "七",
        "env-02": "十一",
        "env-03": "十五",
        "env-04": "十九",
        "env-05": "二十三",
    }


def test_catalog_sources_and_schedules_have_stable_hashes() -> None:
    first = load_eval_catalog(CONFIG_ROOT)
    second = load_eval_catalog(CONFIG_ROOT)

    assert first.source_sha256 == second.source_sha256
    assert set(first.source_sha256) == {
        "cohort_v1.yaml",
        "world_fixtures_v1.yaml",
        "fault_schedules_v1.yaml",
    }
    assert all(len(value) == 64 for value in first.source_sha256.values())
    assert all(len(schedule.sha256) == 64 for schedule in first.fault_schedules.values())


def test_duplicate_case_ids_are_rejected() -> None:
    catalog = load_eval_catalog(CONFIG_ROOT)
    duplicate = catalog.cases[0].model_copy(update={"case_id": catalog.cases[1].case_id})

    with pytest.raises(ValidationError, match="duplicate case_id"):
        EvalCatalog.model_validate(
            {
                "cases": [duplicate, *catalog.cases[1:]],
                "world_fixtures": catalog.world_fixtures,
                "fault_schedules": catalog.fault_schedules,
                "source_sha256": catalog.source_sha256,
            }
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("world_fixture_id", "missing", "unknown world fixture"),
        ("fault_schedule_id", "missing", "unknown fault schedule"),
    ],
)
def test_unknown_catalog_references_are_rejected(field: str, value: str, message: str) -> None:
    catalog = load_eval_catalog(CONFIG_ROOT)
    changed = catalog.cases[0].model_copy(update={field: value})

    with pytest.raises(ValidationError, match=message):
        EvalCatalog.model_validate(
            {
                "cases": [changed, *catalog.cases[1:]],
                "world_fixtures": catalog.world_fixtures,
                "fault_schedules": catalog.fault_schedules,
                "source_sha256": catalog.source_sha256,
            }
        )


def test_non_environment_cases_cannot_arm_faults() -> None:
    catalog = load_eval_catalog(CONFIG_ROOT)
    changed = catalog.cases[0].model_copy(update={"fault_schedule_id": "env-01"})

    with pytest.raises(ValidationError, match="must use fault schedule none"):
        EvalCatalog.model_validate(
            {
                "cases": [changed, *catalog.cases[1:]],
                "world_fixtures": catalog.world_fixtures,
                "fault_schedules": catalog.fault_schedules,
                "source_sha256": catalog.source_sha256,
            }
        )


def test_environment_schedule_mapping_is_frozen() -> None:
    catalog = load_eval_catalog(CONFIG_ROOT)
    environment_case = next(case for case in catalog.cases if case.case_id == "env-01")
    changed = environment_case.model_copy(update={"fault_schedule_id": "env-02"})
    cases = [changed if case.case_id == "env-01" else case for case in catalog.cases]

    with pytest.raises(ValidationError, match="must use matching fault schedule"):
        EvalCatalog.model_validate(
            {
                "cases": cases,
                "world_fixtures": catalog.world_fixtures,
                "fault_schedules": catalog.fault_schedules,
                "source_sha256": catalog.source_sha256,
            }
        )
