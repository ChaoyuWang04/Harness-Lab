from __future__ import annotations

import re
import tomllib
from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]


def test_manifest_declares_m2_observability_dependencies() -> None:
    manifest = tomllib.loads((LAB_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = manifest["project"]["dependencies"]
    joined = "\n".join(dependencies).lower()

    for required in (
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp-proto-grpc",
        "opentelemetry-instrumentation-fastapi",
        "opentelemetry-instrumentation-sqlalchemy",
        "opentelemetry-instrumentation-requests",
        "opentelemetry-instrumentation-httpx",
        "opentelemetry-instrumentation-redis",
        "langfuse",
        "sentry-sdk",
    ):
        assert required in joined
    sentry = next(item for item in dependencies if item.lower().startswith("sentry-sdk"))
    assert "<3" in sentry


def test_pytest_only_collects_lab_owned_tests() -> None:
    manifest = tomllib.loads((LAB_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert manifest["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


def test_compose_adds_lgtm_with_hard_limit_and_lab_persistence() -> None:
    compose = (LAB_ROOT / "compose.yaml").read_text(encoding="utf-8")
    lgtm = re.search(r"(?ms)^  lgtm:\n(.*?)(?=^  \w|\Z)", compose)

    assert lgtm is not None
    block = lgtm.group(1)
    assert "grafana/otel-lgtm:" in block
    assert '"127.0.0.1:3300:3000"' in block
    assert '"4317:4317"' not in block
    assert '"4318:4318"' not in block
    assert "mem_limit: 2g" in block
    assert "./data/lgtm:/data" in block


def test_runtime_services_enable_otlp_without_embedding_secrets() -> None:
    compose = (LAB_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert compose.count('HARNESS_OBSERVABILITY_ENABLED: "true"') == 4
    assert compose.count("OTEL_EXPORTER_OTLP_ENDPOINT: http://lgtm:4317") == 4
    assert "LANGFUSE_SECRET_KEY:" not in compose
    assert "SENTRY_DSN:" not in compose


def test_env_template_has_only_empty_cloud_secret_placeholders() -> None:
    template = (LAB_ROOT / "config" / ".env.example").read_text(encoding="utf-8")

    assert re.search(r"(?m)^SENTRY_DSN=$", template)
    assert re.search(r"(?m)^LANGFUSE_PUBLIC_KEY=$", template)
    assert re.search(r"(?m)^LANGFUSE_SECRET_KEY=$", template)
    assert "HARNESS_OBSERVABILITY_ENABLED=false" in template
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=http://lgtm:4317" in template


def test_compose_keeps_internal_data_services_off_host_ports() -> None:
    compose = (LAB_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert '"5432:5432"' not in compose
    assert '"6379:6379"' not in compose
    assert '"4317:4317"' not in compose
    assert '"4318:4318"' not in compose
    assert '"127.0.0.1:8000:8000"' in compose


def test_compose_declares_gpu_ollama_with_lab_owned_models() -> None:
    compose = (LAB_ROOT / "compose.yaml").read_text(encoding="utf-8")
    ollama = re.search(r"(?ms)^  ollama:\n(.*?)(?=^  \w|\Z)", compose)

    assert ollama is not None
    block = ollama.group(1)
    assert "ollama/ollama:" in block
    assert "./models/ollama:/root/.ollama/models" in block
    assert "capabilities: [gpu]" in block
    assert "driver: nvidia" in block
    assert "count: 1" in block
    assert "ports:" not in block
