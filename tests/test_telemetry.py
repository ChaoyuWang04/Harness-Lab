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
