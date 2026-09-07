from __future__ import annotations

import asyncio
import hashlib
import threading
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx2
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from app.config import settings


@dataclass(frozen=True, slots=True)
class ChaosConfig:
    upstream_url: str = "http://ollama:11434"
    seed: int = 0
    rate_429: float = 0
    rate_timeout: float = 0
    rate_5xx: float = 0
    latency_ms: int = 0
    timeout_seconds: float = 31

    def __post_init__(self) -> None:
        rates = (self.rate_429, self.rate_timeout, self.rate_5xx)
        if any(rate < 0 or rate > 1 for rate in rates) or sum(rates) > 1:
            raise ValueError("chaos fault rates must be in [0, 1] and sum to at most 1")
        if self.latency_ms < 0 or self.timeout_seconds <= 0:
            raise ValueError("chaos latency and timeout must be non-negative")

    @classmethod
    def from_settings(cls) -> "ChaosConfig":
        return cls(
            upstream_url=settings.chaos_upstream_url,
            seed=settings.chaos_seed,
            rate_429=settings.chaos_429_rate,
            rate_timeout=settings.chaos_timeout_rate,
            rate_5xx=settings.chaos_5xx_rate,
            latency_ms=settings.chaos_latency_ms,
            timeout_seconds=settings.chaos_timeout_seconds,
        )


class FaultSchedule:
    def __init__(self, config: ChaosConfig) -> None:
        self.config = config
        self._request_index = 0
        self._counts: Counter[str] = Counter()
        self._lock = threading.Lock()

    def decide(self) -> str:
        with self._lock:
            index = self._request_index
            self._request_index += 1
            digest = hashlib.sha256(f"{self.config.seed}:{index}".encode("ascii")).digest()
            sample = int.from_bytes(digest[:8], "big") / 2**64
            if sample < self.config.rate_429:
                decision = "429"
            elif sample < self.config.rate_429 + self.config.rate_timeout:
                decision = "timeout"
            elif sample < self.config.rate_429 + self.config.rate_timeout + self.config.rate_5xx:
                decision = "5xx"
            else:
                decision = "forward"
            self._counts[decision] += 1
            return decision

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "requests": self._request_index,
                "counts": {
                    name: self._counts[name] for name in ("forward", "429", "timeout", "5xx")
                },
            }


Forwarder = Callable[[dict[str, object]], Awaitable[tuple[int, dict[str, str], bytes]]]
Sleeper = Callable[[float], Awaitable[None]]


def create_app(
    *,
    config: ChaosConfig | None = None,
    forward: Forwarder | None = None,
    sleep: Sleeper = asyncio.sleep,
) -> FastAPI:
    active = config or ChaosConfig.from_settings()
    schedule = FaultSchedule(active)

    async def forward_upstream(body: dict[str, object]) -> tuple[int, dict[str, str], bytes]:
        async with httpx2.AsyncClient(trust_env=False, timeout=180) as client:
            response = await client.post(
                active.upstream_url.rstrip("/") + "/v1/chat/completions",
                json=body,
            )
        content_type = response.headers.get("content-type", "application/json")
        return response.status_code, {"content-type": content_type}, response.content

    forward_request = forward or forward_upstream
    application = FastAPI(title="Harness Lab Chaos Proxy")

    @application.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "faults_enabled": any(
                (
                    active.rate_429,
                    active.rate_timeout,
                    active.rate_5xx,
                    active.latency_ms,
                )
            ),
            "seed": active.seed,
            "rates": {
                "429": active.rate_429,
                "timeout": active.rate_timeout,
                "5xx": active.rate_5xx,
            },
            "latency_ms": active.latency_ms,
            **schedule.snapshot(),
        }

    @application.post("/v1/chat/completions")
    async def chat_completions(body: dict[str, object]) -> Response:
        if active.latency_ms:
            await sleep(active.latency_ms / 1000)
        decision = schedule.decide()
        if decision == "429":
            return JSONResponse(
                {"error": {"message": "M3 injected rate limit", "type": "rate_limit_error"}},
                status_code=429,
            )
        if decision == "timeout":
            await sleep(active.timeout_seconds)
            return JSONResponse(
                {"error": {"message": "M3 injected timeout", "type": "timeout"}},
                status_code=504,
            )
        if decision == "5xx":
            return JSONResponse(
                {"error": {"message": "M3 injected provider failure", "type": "server_error"}},
                status_code=503,
            )
        status, headers, content = await forward_request(body)
        return Response(
            content=content,
            status_code=status,
            media_type=headers.get("content-type", "application/json").split(";", 1)[0],
        )

    return application


app = create_app()
