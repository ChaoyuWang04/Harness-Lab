from __future__ import annotations

import re
import sys
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.config import Settings  # noqa: E402


def _service_block(compose: str, service: str) -> str:
    match = re.search(rf"(?ms)^  {service}:\n(.*?)(?=^  \w|\Z)", compose)
    assert match is not None
    return match.group(1)


def test_m3_settings_are_bounded_and_disabled_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.llm_timeout_seconds == 30
    assert settings.llm_max_retries == 3
    assert settings.llm_retry_base_seconds == 0.5
    assert settings.chaos_upstream_url == "http://ollama:11434"
    assert settings.chaos_seed == 0
    assert settings.chaos_429_rate == 0
    assert settings.chaos_timeout_rate == 0
    assert settings.chaos_5xx_rate == 0
    assert settings.chaos_latency_ms == 0
    assert settings.chaos_timeout_seconds == 31


def test_compose_has_profile_gated_proxy_and_isolated_runtime_overrides() -> None:
    compose = (LAB_ROOT / "compose.yaml").read_text(encoding="utf-8")
    proxy = _service_block(compose, "chaos-proxy")

    assert 'profiles: ["m3"]' in proxy
    assert "uvicorn app.chaos.proxy:app" in proxy
    assert "CHAOS_UPSTREAM_URL: http://ollama:11434" in proxy
    assert "mem_limit: 256m" in proxy
    assert "ports:" not in proxy

    assert "HARNESS_COMPOSE_DATABASE_URL" in _service_block(compose, "migrate")
    for service in ("api", "dispatcher", "worker", "sweeper"):
        block = _service_block(compose, service)
        assert "HARNESS_COMPOSE_DATABASE_URL" in block
        assert "HARNESS_COMPOSE_REDIS_URL" in block


def test_example_env_documents_m3_without_enabling_faults() -> None:
    example = (LAB_ROOT / "config" / ".env.example").read_text(encoding="utf-8")

    for key in (
        "LLM_TIMEOUT_SECONDS=30",
        "LLM_MAX_RETRIES=3",
        "LLM_RETRY_BASE_SECONDS=0.5",
        "CHAOS_SEED=0",
        "CHAOS_429_RATE=0",
        "CHAOS_TIMEOUT_RATE=0",
        "CHAOS_5XX_RATE=0",
        "CHAOS_LATENCY_MS=0",
        "CHAOS_TIMEOUT_SECONDS=31",
    ):
        assert key in example
