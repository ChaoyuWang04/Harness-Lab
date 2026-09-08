from __future__ import annotations

import sys
from pathlib import Path

import pytest


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))


def test_disabled_observability_is_non_fatal_and_idempotent() -> None:
    from app.telemetry import initialize_observability

    first = initialize_observability("unit-test", enabled=False)
    second = initialize_observability("unit-test", enabled=False)

    assert first.enabled is False
    assert second.enabled is False
    first.force_flush()
    first.shutdown()


def test_observability_initialization_failure_is_non_fatal() -> None:
    from app import telemetry

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(telemetry, "TracerProvider", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
        runtime = telemetry.initialize_observability("broken-init", enabled=True)

    assert runtime.enabled is False


def test_observability_can_keep_cloud_sinks_without_local_otel_export() -> None:
    from app import telemetry

    def unexpected_exporter(**_kwargs):
        raise AssertionError("local OTLP exporter must remain disabled")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(telemetry.settings, "harness_otel_export_enabled", False)
        patch.setattr(telemetry, "OTLPSpanExporter", unexpected_exporter)
        patch.setattr(telemetry, "OTLPMetricExporter", unexpected_exporter)
        patch.setattr(telemetry, "_instrument_clients", lambda _engine: None)
        patch.setattr(telemetry, "_init_sentry", lambda: None)
        patch.setattr(telemetry.trace, "set_tracer_provider", lambda _provider: None)
        patch.setattr(telemetry.metrics, "set_meter_provider", lambda _provider: None)
        patch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        patch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        runtime = telemetry.initialize_observability("cloud-only-test", enabled=True)

    assert runtime.enabled is True
    runtime.shutdown()


def test_metric_labels_are_bounded() -> None:
    from app.telemetry import validate_error_code, validate_http_status, validate_run_status

    assert validate_run_status("completed") == "completed"
    assert validate_error_code("MODEL_TIMEOUT") == "MODEL_TIMEOUT"
    assert validate_http_status("429") == "429"
    with pytest.raises(ValueError):
        validate_run_status("run_123")
    with pytest.raises(ValueError):
        validate_http_status("503")


def test_sentry_scrubber_removes_request_secrets_but_keeps_run_id() -> None:
    from app.telemetry import scrub_sentry_event

    event = {
        "request": {
            "headers": {"authorization": "Bearer secret", "cookie": "session=secret", "x-ok": "ok"},
            "data": {"prompt": "private"},
        },
        "tags": {"run_id": "run_123"},
    }

    scrubbed = scrub_sentry_event(event, {})

    assert scrubbed["request"]["headers"] == {"x-ok": "ok"}
    assert "data" not in scrubbed["request"]
    assert scrubbed["tags"]["run_id"] == "run_123"
