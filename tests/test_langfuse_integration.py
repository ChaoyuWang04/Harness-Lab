from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))


class Observation:
    def __init__(self) -> None:
        self.updated = None

    def update(self, **kwargs) -> None:
        self.updated = kwargs


class Langfuse:
    def __init__(self) -> None:
        self.started = None
        self.observation = Observation()

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        self.started = kwargs
        yield self.observation


def test_generation_records_run_prompt_completion_and_all_token_counts() -> None:
    from app.telemetry import model_observation, update_model_observation

    client = Langfuse()
    messages = [{"role": "user", "content": "你好"}]

    with model_observation("run_123", 2, messages, "qwen3:0.6b", client=client) as observation:
        update_model_observation(
            observation,
            output={"content": "您好", "tool_calls": []},
            usage={"input": 11, "output": 4, "total": 15},
        )

    assert client.started == {
        "as_type": "generation",
        "name": "harness.model_call",
        "input": messages,
        "model": "qwen3:0.6b",
        "metadata": {"run_id": "run_123", "step": 2},
    }
    assert client.observation.updated == {
        "output": {"content": "您好", "tool_calls": []},
        "usage_details": {"input": 11, "output": 4, "total": 15},
    }
