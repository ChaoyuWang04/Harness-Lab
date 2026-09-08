from __future__ import annotations

import asyncio
import hashlib
import threading
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx2
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, Response

from app.config import settings
from app.eval.catalog import EvalCatalog, load_eval_catalog


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


class RegisteredFaultWindow:
    def __init__(self, catalog: EvalCatalog) -> None:
        self.catalog = catalog
        self._case_id: str | None = None
        self._decisions: list[str] = []
        self._cursor = 0
        self._registered_counts: Counter[str] = Counter()
        self._registered_decisions: list[str] = []
        self._post_schedule_model_attempts = 0
        self._injection_window_closed = False
        self._lock = threading.Lock()

    def arm(self, case_id: str) -> dict[str, Any]:
        with self._lock:
            if self._case_id is not None:
                raise RuntimeError("a fault schedule is already active")
            case = next((item for item in self.catalog.cases if item.case_id == case_id), None)
            if case is None:
                raise KeyError(case_id)
            self._case_id = case_id
            self._decisions = (
                list(self.catalog.fault_schedules[case.fault_schedule_id].decisions)
                if case.scenario_kind == "environment"
                else []
            )
            self._cursor = 0
            self._registered_counts.clear()
            self._registered_decisions.clear()
            self._post_schedule_model_attempts = 0
            self._injection_window_closed = case.scenario_kind != "environment"
            return self._snapshot_unlocked()

    def decide(self) -> str:
        with self._lock:
            if self._case_id is None:
                raise RuntimeError("no M4 fault schedule is armed")
            if self._injection_window_closed:
                self._post_schedule_model_attempts += 1
                return "forward"
            if self._cursor >= len(self._decisions):
                return "exhausted"
            decision = self._decisions[self._cursor]
            self._cursor += 1
            self._registered_counts[decision] += 1
            self._registered_decisions.append(decision)
            if decision == "200":
                self._injection_window_closed = True
            return decision

    def disarm(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._case_id is None:
                return self._snapshot_unlocked()
            fully_consumed = self._cursor == len(self._decisions)
            if not force and not (self._injection_window_closed or fully_consumed):
                raise RuntimeError("fault schedule is only partially consumed")
            previous = self._snapshot_unlocked()
            self._case_id = None
            self._decisions = []
            self._cursor = 0
            self._registered_counts.clear()
            self._registered_decisions.clear()
            self._post_schedule_model_attempts = 0
            self._injection_window_closed = False
            return previous

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict[str, Any]:
        return {
            "case_id": self._case_id,
            "cursor": self._cursor,
            "expected_length": len(self._decisions),
            "registered_counts": {
                status: self._registered_counts[status]
                for status in ("200", "429", "timeout", "503")
            },
            "registered_decisions": list(self._registered_decisions),
            "post_schedule_model_attempts": self._post_schedule_model_attempts,
            "injection_window_closed": self._injection_window_closed,
        }


Forwarder = Callable[[dict[str, object]], Awaitable[tuple[int, dict[str, str], bytes]]]
Sleeper = Callable[[float], Awaitable[None]]


def create_app(
    *,
    config: ChaosConfig | None = None,
    forward: Forwarder | None = None,
    sleep: Sleeper = asyncio.sleep,
    m4_eval_mode: bool | None = None,
    control_token: str | None = None,
    eval_catalog: EvalCatalog | None = None,
) -> FastAPI:
    active = config or ChaosConfig.from_settings()
    schedule = FaultSchedule(active)
    eval_mode = settings.harness_m4_eval_mode if m4_eval_mode is None else m4_eval_mode
    token = settings.harness_m4_control_token if control_token is None else control_token
    registered_window: RegisteredFaultWindow | None = None
    if eval_mode:
        if not token:
            raise ValueError("M4 eval mode requires a non-empty control token")
        catalog = eval_catalog or load_eval_catalog(settings.harness_m4_catalog_path)
        registered_window = RegisteredFaultWindow(catalog)

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
            "m4_eval_mode": eval_mode,
            **schedule.snapshot(),
        }

    if registered_window is not None:

        def authorize(x_harness_m4_token: str | None = Header(default=None)) -> None:
            if x_harness_m4_token != token:
                raise HTTPException(status_code=401, detail="unauthorized")

        @application.post("/internal/eval/arm/{case_id}")
        def arm_schedule(case_id: str, _: None = Depends(authorize)) -> dict[str, Any]:
            try:
                return registered_window.arm(case_id)
            except KeyError as error:
                raise HTTPException(status_code=404, detail="unknown case") from error
            except RuntimeError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

        @application.get("/internal/eval/state")
        def schedule_state(_: None = Depends(authorize)) -> dict[str, Any]:
            return registered_window.snapshot()

        @application.post("/internal/eval/disarm")
        def disarm_schedule(
            force: bool = False,
            _: None = Depends(authorize),
        ) -> dict[str, Any]:
            try:
                return registered_window.disarm(force=force)
            except RuntimeError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/v1/chat/completions")
    async def chat_completions(body: dict[str, object]) -> Response:
        if active.latency_ms:
            await sleep(active.latency_ms / 1000)
        try:
            decision = (
                registered_window.decide()
                if registered_window is not None
                else schedule.decide()
            )
        except RuntimeError as error:
            return JSONResponse(
                {"error": {"message": str(error), "type": "schedule_error"}},
                status_code=409,
            )
        if decision == "exhausted":
            return JSONResponse(
                {"error": {"message": "M4 registered schedule exhausted", "type": "schedule_error"}},
                status_code=409,
            )
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
        if decision in {"5xx", "503"}:
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
