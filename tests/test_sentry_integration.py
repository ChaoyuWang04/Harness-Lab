from __future__ import annotations

import sys
import importlib.util
from pathlib import Path
from unittest import mock


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))


def load_verifier():
    path = LAB_ROOT / "scripts" / "verify_sentry.py"
    spec = importlib.util.spec_from_file_location("verify_sentry", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_capture_exception_sets_run_id_and_returns_event_id() -> None:
    from app.telemetry import capture_sentry_exception

    scope = mock.MagicMock()
    context = mock.MagicMock()
    context.__enter__.return_value = scope
    with (
        mock.patch("sentry_sdk.new_scope", return_value=context),
        mock.patch("sentry_sdk.capture_exception", return_value="event-123") as capture,
    ):
        event_id = capture_sentry_exception(RuntimeError("probe"), "run_probe")

    scope.set_tag.assert_called_once_with("run_id", "run_probe")
    capture.assert_called_once()
    assert event_id == "event-123"


def test_rq_workhorse_always_flushes_and_shuts_down() -> None:
    from app import jobs

    runtime = mock.MagicMock(enabled=True)
    with (
        mock.patch.object(jobs, "initialize_observability", return_value=runtime),
        mock.patch.object(jobs, "_execute_run_body", return_value=True),
        mock.patch.object(jobs, "span_from_carrier") as span,
    ):
        span.return_value.__enter__.return_value = mock.MagicMock()
        assert jobs.execute_run("run_flush") is True

    runtime.force_flush.assert_called_once_with()
    runtime.shutdown.assert_called_once_with()


def test_rq_workhorse_telemetry_cleanup_failure_does_not_fail_the_run() -> None:
    from app import jobs

    runtime = mock.MagicMock(enabled=True)
    runtime.force_flush.side_effect = RuntimeError("collector unavailable")
    runtime.shutdown.side_effect = RuntimeError("shutdown unavailable")
    with (
        mock.patch.object(jobs, "initialize_observability", return_value=runtime),
        mock.patch.object(jobs, "_execute_run_body", return_value=True),
        mock.patch.object(jobs, "span_from_carrier") as span,
    ):
        span.return_value.__enter__.return_value = mock.MagicMock()
        assert jobs.execute_run("run_flush_failure") is True

    runtime.force_flush.assert_called_once_with()
    runtime.shutdown.assert_called_once_with()


def test_sentry_verifier_is_opt_in_and_returns_only_safe_identity() -> None:
    verifier = load_verifier()
    fake = mock.MagicMock()
    fake.capture_exception.return_value = "event-456"
    scope = mock.MagicMock()
    fake.new_scope.return_value.__enter__.return_value = scope

    result = verifier.run_probe("run_sentry_probe", "https://public@example.invalid/1", sentry=fake)

    scope.set_tag.assert_called_once_with("run_id", "run_sentry_probe")
    fake.flush.assert_called_once()
    assert result == {"event_id": "event-456", "run_id": "run_sentry_probe", "sent": True}
