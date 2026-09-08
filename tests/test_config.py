import sys
from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.config import Settings  # noqa: E402


def test_observability_is_safe_by_default_for_unit_tests() -> None:
    settings = Settings(_env_file=None)

    assert settings.harness_observability_enabled is False
    assert settings.harness_otel_export_enabled is True
    assert settings.otel_exporter_otlp_endpoint == "http://lgtm:4317"
    assert settings.otel_metric_export_interval_ms == 5000
    assert settings.otel_export_timeout_ms == 3000
    assert settings.capture_model_turns is False
