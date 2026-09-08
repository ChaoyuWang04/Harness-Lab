from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


LAB_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=LAB_ROOT / "secrets" / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://postgres:harness@postgres:5432/harness"
    test_database_url: str = "postgresql+psycopg://postgres:harness@127.0.0.1:5432/harness"
    redis_url: str = "redis://redis:6379/0"
    test_redis_url: str = "redis://127.0.0.1:6379/0"
    llm_base_url: str = "http://ollama:11434/v1"
    llm_model: str = "qwen3:0.6b"
    llm_timeout_seconds: float = Field(default=30, gt=0, le=120)
    llm_max_retries: int = Field(default=3, ge=0, le=3)
    llm_retry_base_seconds: float = Field(default=0.5, ge=0, le=10)
    lease_seconds: int = Field(default=30, ge=5)
    max_attempts: int = Field(default=3, ge=1)
    harness_test_pause_after_tool_seconds: int = Field(default=0, ge=0, le=120)
    capture_model_turns: bool = False
    harness_observability_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://lgtm:4317"
    otel_metric_export_interval_ms: int = Field(default=5000, ge=1000)
    otel_export_timeout_ms: int = Field(default=3000, ge=100, le=30000)
    chaos_upstream_url: str = "http://ollama:11434"
    chaos_seed: int = 0
    chaos_429_rate: float = Field(default=0, ge=0, le=1)
    chaos_timeout_rate: float = Field(default=0, ge=0, le=1)
    chaos_5xx_rate: float = Field(default=0, ge=0, le=1)
    chaos_latency_ms: int = Field(default=0, ge=0, le=60000)
    chaos_timeout_seconds: float = Field(default=31, gt=0, le=180)
    chaos_dispatcher_crash_after_publish: bool = False
    chaos_dispatcher_marker: Path = Path("/var/lib/harness-chaos/dispatcher-after-publish.once")


settings = Settings()
