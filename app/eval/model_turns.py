from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.fencing import WorkerFence, lock_current_run
from app.models import ModelTurnRecord


MODEL_TURN_ERROR_CODES = frozenset(
    {"MODEL_429", "MODEL_TIMEOUT", "MODEL_5XX", "MODEL_INTERNAL"}
)


def record_model_turn(
    session_factory: sessionmaker[Session],
    fence: WorkerFence,
    run_id: str,
    *,
    step: int,
    model_attempt: int,
    input_messages: list[dict[str, Any]],
    output_message: dict[str, Any] | None,
    usage: dict[str, int] | None,
    error_code: str | None,
    enabled: bool | None = None,
) -> bool:
    capture = settings.capture_model_turns if enabled is None else enabled
    if not capture:
        return False
    if error_code is not None and error_code not in MODEL_TURN_ERROR_CODES:
        raise ValueError(f"unsupported model turn error code: {error_code}")

    with session_factory.begin() as session:
        run = lock_current_run(session, run_id, fence)
        if run is None:
            raise RuntimeError("model turn belongs to a stale worker generation")
        session.add(
            ModelTurnRecord(
                run_id=run_id,
                run_attempt=fence.attempt,
                step=step,
                model_attempt=model_attempt,
                prompt_version=run.prompt_version,
                input_messages_json=deepcopy(input_messages),
                output_message_json=deepcopy(output_message),
                usage_json=deepcopy(usage),
                error_code=error_code,
            )
        )
    return True
