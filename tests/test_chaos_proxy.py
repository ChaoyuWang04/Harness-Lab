from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastapi.testclient import TestClient


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.chaos.proxy import ChaosConfig, FaultSchedule, create_app  # noqa: E402
from app.eval.catalog import load_eval_catalog  # noqa: E402


EVAL_CONFIG = LAB_ROOT / "config" / "eval"
CONTROL_HEADERS = {"x-harness-m4-token": "test-token"}


def test_seeded_fault_schedule_is_reproducible_and_bounded() -> None:
    config = ChaosConfig(seed=42, rate_429=0.5, rate_timeout=0.3, rate_5xx=0.1)

    first = [FaultSchedule(config).decide() for _ in range(1)]
    first_schedule = FaultSchedule(config)
    second_schedule = FaultSchedule(config)
    sequence_a = [first_schedule.decide() for _ in range(50)]
    sequence_b = [second_schedule.decide() for _ in range(50)]

    assert first[0] in {"forward", "429", "timeout", "5xx"}
    assert sequence_a == sequence_b
    assert set(sequence_a) <= {"forward", "429", "timeout", "5xx"}
    assert first_schedule.snapshot()["requests"] == 50


def test_proxy_forwards_openai_body_and_reports_non_secret_health() -> None:
    seen: list[dict[str, object]] = []

    async def forward(body: dict[str, object]):
        seen.append(body)
        return 200, {"content-type": "application/json"}, b'{"choices":[]}'

    app = create_app(config=ChaosConfig(), forward=forward)
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json={"model": "tiny", "messages": []})
        health = client.get("/health").json()

    assert response.status_code == 200
    assert response.json() == {"choices": []}
    assert seen == [{"model": "tiny", "messages": []}]
    assert health["faults_enabled"] is False
    assert health["counts"]["forward"] == 1
    assert "upstream_url" not in health


def test_proxy_injects_each_fault_without_calling_upstream() -> None:
    calls = 0

    async def forward(_body: dict[str, object]):
        nonlocal calls
        calls += 1
        return 200, {}, b"{}"

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(0)

    cases = [
        (ChaosConfig(rate_429=1), 429),
        (ChaosConfig(rate_timeout=1, timeout_seconds=0.01), 504),
        (ChaosConfig(rate_5xx=1), 503),
    ]
    for config, status in cases:
        with TestClient(create_app(config=config, forward=forward, sleep=sleep)) as client:
            assert client.post("/v1/chat/completions", json={}).status_code == status

    assert calls == 0
    assert sleeps == [0.01]


def test_latency_is_applied_before_forwarding() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def forward(_body: dict[str, object]):
        return 200, {}, b"{}"

    with TestClient(
        create_app(config=ChaosConfig(latency_ms=300), forward=forward, sleep=sleep)
    ) as client:
        assert client.post("/v1/chat/completions", json={}).status_code == 200

    assert sleeps == [0.3]


def test_m4_control_routes_are_absent_when_eval_mode_is_disabled() -> None:
    with TestClient(create_app(config=ChaosConfig())) as client:
        assert client.get("/internal/eval/state").status_code == 404
        assert client.post("/internal/eval/arm/env-01").status_code == 404


def test_m4_control_requires_token_and_catalog_case() -> None:
    app = create_app(
        config=ChaosConfig(),
        m4_eval_mode=True,
        control_token="test-token",
        eval_catalog=load_eval_catalog(EVAL_CONFIG),
    )
    with TestClient(app) as client:
        assert client.get("/internal/eval/state").status_code == 401
        assert client.get(
            "/internal/eval/state", headers={"x-harness-m4-token": "wrong"}
        ).status_code == 401
        assert client.post(
            "/internal/eval/arm/not-registered", headers=CONTROL_HEADERS
        ).status_code == 404
        assert client.post("/v1/chat/completions", json={}).status_code == 409


def test_m4_schedule_consumes_registered_window_then_forwards_later_rounds() -> None:
    upstream_calls: list[dict[str, object]] = []

    async def forward(body: dict[str, object]):
        upstream_calls.append(body)
        return 200, {"content-type": "application/json"}, b'{"choices":[]}'

    app = create_app(
        config=ChaosConfig(),
        forward=forward,
        m4_eval_mode=True,
        control_token="test-token",
        eval_catalog=load_eval_catalog(EVAL_CONFIG),
    )
    with TestClient(app) as client:
        assert client.post("/internal/eval/arm/env-01", headers=CONTROL_HEADERS).status_code == 200
        assert client.post("/internal/eval/arm/env-02", headers=CONTROL_HEADERS).status_code == 409
        statuses = [
            client.post("/v1/chat/completions", json={"request": index}).status_code
            for index in range(4)
        ]
        state = client.get("/internal/eval/state", headers=CONTROL_HEADERS).json()
        assert client.post("/internal/eval/disarm", headers=CONTROL_HEADERS).status_code == 200

    assert statuses == [429, 429, 200, 200]
    assert upstream_calls == [{"request": 2}, {"request": 3}]
    assert state["case_id"] == "env-01"
    assert state["cursor"] == 3
    assert state["expected_length"] == 3
    assert state["registered_counts"] == {"200": 1, "429": 2, "timeout": 0, "503": 0}
    assert state["registered_decisions"] == ["429", "429", "200"]
    assert state["post_schedule_model_attempts"] == 1
    assert state["injection_window_closed"] is True
    assert "token" not in str(state).lower()
    assert "upstream" not in str(state).lower()


def test_m4_all_error_schedule_fails_closed_after_exact_exhaustion() -> None:
    app = create_app(
        config=ChaosConfig(),
        m4_eval_mode=True,
        control_token="test-token",
        eval_catalog=load_eval_catalog(EVAL_CONFIG),
    )
    with TestClient(app) as client:
        assert client.post("/internal/eval/arm/env-06", headers=CONTROL_HEADERS).status_code == 200
        statuses = [client.post("/v1/chat/completions", json={}).status_code for _ in range(4)]
        assert client.post("/v1/chat/completions", json={}).status_code == 409
        state = client.get("/internal/eval/state", headers=CONTROL_HEADERS).json()
        assert client.post("/internal/eval/disarm", headers=CONTROL_HEADERS).status_code == 200

    assert statuses == [429, 429, 429, 429]
    assert state["cursor"] == state["expected_length"] == 4
    assert state["injection_window_closed"] is False


def test_m4_disarm_refuses_partially_consumed_schedule_unless_forced() -> None:
    app = create_app(
        config=ChaosConfig(),
        m4_eval_mode=True,
        control_token="test-token",
        eval_catalog=load_eval_catalog(EVAL_CONFIG),
    )
    with TestClient(app) as client:
        assert client.post("/internal/eval/arm/env-03", headers=CONTROL_HEADERS).status_code == 200
        assert client.post("/v1/chat/completions", json={}).status_code == 503
        assert client.post("/internal/eval/disarm", headers=CONTROL_HEADERS).status_code == 409
        assert client.post(
            "/internal/eval/disarm?force=true", headers=CONTROL_HEADERS
        ).status_code == 200


def test_m4_registered_normal_case_forwards_without_fault_decisions() -> None:
    calls = 0

    async def forward(_body: dict[str, object]):
        nonlocal calls
        calls += 1
        return 200, {"content-type": "application/json"}, b"{}"

    app = create_app(
        config=ChaosConfig(),
        forward=forward,
        m4_eval_mode=True,
        control_token="test-token",
        eval_catalog=load_eval_catalog(EVAL_CONFIG),
    )
    with TestClient(app) as client:
        assert client.post("/internal/eval/arm/normal-01", headers=CONTROL_HEADERS).status_code == 200
        assert client.post("/v1/chat/completions", json={}).status_code == 200
        state = client.get("/internal/eval/state", headers=CONTROL_HEADERS).json()
        assert client.post("/internal/eval/disarm", headers=CONTROL_HEADERS).status_code == 200

    assert calls == 1
    assert state["expected_length"] == 0
    assert state["registered_counts"] == {"200": 0, "429": 0, "timeout": 0, "503": 0}
    assert state["post_schedule_model_attempts"] == 1
