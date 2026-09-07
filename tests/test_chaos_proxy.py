from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastapi.testclient import TestClient


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.chaos.proxy import ChaosConfig, FaultSchedule, create_app  # noqa: E402


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
