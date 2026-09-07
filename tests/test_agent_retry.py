from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path

import httpx2
import pytest
from openai import APITimeoutError, RateLimitError


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.agent.llm import ModelTurn  # noqa: E402
from app.agent.loop import run_agent  # noqa: E402
from app.fencing import WorkerFence  # noqa: E402


class FakeSessions:
    def begin(self):
        return nullcontext(object())


class RetryClient:
    model = "test-model"

    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    def complete(self, _messages, _tools):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RecordingMetrics:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    def record_model_call(self, status: str) -> None:
        self.statuses.append(status)


def _rate_limit() -> RateLimitError:
    request = httpx2.Request("POST", "http://model.invalid/v1/chat/completions")
    response = httpx2.Response(429, request=request)
    return RateLimitError("limited", response=response, body=None)


def _patch_runtime(monkeypatch: pytest.MonkeyPatch):
    events: list[tuple[str, dict[str, object]]] = []
    observations: list[object] = []
    metrics = RecordingMetrics()
    monkeypatch.setattr(
        "app.agent.loop.append_event",
        lambda _session, _run, event, payload, **_kwargs: events.append((event, payload)),
    )

    def observation(*_args, **_kwargs):
        marker = object()
        observations.append(marker)
        return nullcontext(marker)

    monkeypatch.setattr("app.agent.loop.model_observation", observation)
    monkeypatch.setattr("app.agent.loop.update_model_observation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.agent.loop.get_harness_metrics", lambda: metrics)
    return events, observations, metrics


def test_rate_limit_retries_three_times_with_exponential_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    events, observations, metrics = _patch_runtime(monkeypatch)
    sleeps: list[float] = []
    client = RetryClient([_rate_limit(), _rate_limit(), ModelTurn("ok", [], {"total": 1})])

    result = run_agent(
        FakeSessions(),
        WorkerFence("worker", 1),
        "run-retry",
        "hello",
        client,
        max_model_retries=3,
        retry_base_seconds=0.5,
        sleep=sleeps.append,
    )

    assert result == {"answer": "ok"}
    assert client.calls == 3
    assert len(observations) == 3
    assert metrics.statuses == ["429", "429", "200"]
    assert sleeps == [0.5, 1.0]
    assert [name for name, _ in events] == [
        "step.model_call",
        "step.model_retry",
        "step.model_retry",
    ]
    assert [payload["error_code"] for name, payload in events if name == "step.model_retry"] == [
        "MODEL_429",
        "MODEL_429",
    ]


def test_timeout_stops_after_three_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    events, observations, metrics = _patch_runtime(monkeypatch)
    request = httpx2.Request("POST", "http://model.invalid/v1/chat/completions")
    client = RetryClient([APITimeoutError(request) for _ in range(4)])

    with pytest.raises(APITimeoutError):
        run_agent(
            FakeSessions(),
            WorkerFence("worker", 1),
            "run-timeout",
            "hello",
            client,
            max_model_retries=3,
            retry_base_seconds=0,
            sleep=lambda _seconds: None,
        )

    assert client.calls == 4
    assert len(observations) == 4
    assert metrics.statuses == ["timeout"] * 4
    assert len([event for event, _ in events if event == "step.model_retry"]) == 3
