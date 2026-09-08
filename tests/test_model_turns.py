from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.eval.model_turns import record_model_turn  # noqa: E402
from app.fencing import WorkerFence  # noqa: E402
from app.models import ModelTurnRecord  # noqa: E402


def _unique_columns(table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }


def test_model_turn_identity_includes_worker_generation_and_model_attempt() -> None:
    assert _unique_columns(ModelTurnRecord.__table__) == {
        ("run_id", "run_attempt", "step", "model_attempt")
    }
    assert ModelTurnRecord.__table__.c.created_at.type.timezone is True


class ExplodingSessions:
    def begin(self):
        raise AssertionError("capture-disabled recording opened a transaction")


def test_capture_disabled_does_not_open_a_transaction() -> None:
    assert (
        record_model_turn(
            ExplodingSessions(),
            WorkerFence("worker-a", 1),
            "run-1",
            step=1,
            model_attempt=0,
            input_messages=[{"role": "user", "content": "hello"}],
            output_message={"content": "hi", "tool_calls": []},
            usage={"total": 2},
            error_code=None,
            enabled=False,
        )
        is False
    )


class RecordingSession:
    def __init__(self) -> None:
        self.items: list[object] = []

    def add(self, item: object) -> None:
        self.items.append(item)


class RecordingSessions:
    def __init__(self) -> None:
        self.session = RecordingSession()

    def begin(self):
        return nullcontext(self.session)


def test_capture_enabled_records_stable_fields(monkeypatch) -> None:
    sessions = RecordingSessions()
    monkeypatch.setattr(
        "app.eval.model_turns.lock_current_run",
        lambda *_args: SimpleNamespace(prompt_version="v1"),
    )

    recorded = record_model_turn(
        sessions,
        WorkerFence("worker-a", 2),
        "run-1",
        step=3,
        model_attempt=1,
        input_messages=[{"role": "user", "content": "hello"}],
        output_message={"content": "hi", "tool_calls": []},
        usage={"total": 2},
        error_code=None,
        enabled=True,
    )

    assert recorded is True
    [item] = sessions.session.items
    assert item.run_id == "run-1"
    assert item.run_attempt == 2
    assert item.step == 3
    assert item.model_attempt == 1
    assert item.input_messages_json == [{"role": "user", "content": "hello"}]


def test_capture_rejects_unbounded_error_text(monkeypatch) -> None:
    sessions = RecordingSessions()
    monkeypatch.setattr(
        "app.eval.model_turns.lock_current_run",
        lambda *_args: SimpleNamespace(prompt_version="v1"),
    )

    try:
        record_model_turn(
            sessions,
            WorkerFence("worker-a", 1),
            "run-1",
            step=1,
            model_attempt=0,
            input_messages=[],
            output_message=None,
            usage=None,
            error_code="https://secret.invalid/token=abc",
            enabled=True,
        )
    except ValueError as error:
        assert "unsupported model turn error code" in str(error)
    else:
        raise AssertionError("unbounded error text was accepted")
