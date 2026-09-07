from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))


class Instrument:
    def __init__(self) -> None:
        self.calls = []

    def add(self, value, attributes=None) -> None:
        self.calls.append((value, attributes))

    def record(self, value, attributes=None) -> None:
        self.calls.append((value, attributes))


class Meter:
    def __init__(self) -> None:
        self.instruments = {}

    def _make(self, name, **_kwargs):
        instrument = Instrument()
        self.instruments[name] = instrument
        return instrument

    create_counter = _make
    create_histogram = _make


def test_metric_recorder_uses_exact_names_units_and_bounded_labels() -> None:
    from app.telemetry import HarnessMetrics

    meter = Meter()
    recorder = HarnessMetrics(meter)

    recorder.record_queue_lag(1.25)
    recorder.record_run_outcome(2.5, "failed", "MODEL_TIMEOUT")
    recorder.record_model_call("429")
    recorder.record_tool_latency("get_report", 7.5, executed_now=True)
    recorder.record_tool_latency("get_report", 99, executed_now=False)
    recorder.record_sweep("requeued")

    assert meter.instruments["agent_queue_lag_seconds"].calls == [(1.25, None)]
    assert meter.instruments["agent_run_duration_seconds"].calls == [(2.5, {"status": "failed"})]
    assert meter.instruments["agent_run_total"].calls == [(1, {"status": "failed"})]
    assert meter.instruments["agent_run_failed_total"].calls == [(1, {"error_code": "MODEL_TIMEOUT"})]
    assert meter.instruments["agent_model_call_total"].calls == [(1, {"http_status": "429"})]
    assert meter.instruments["agent_tool_latency_ms"].calls == [(7.5, {"tool_name": "get_report"})]
    assert meter.instruments["stuck_runs_swept_total"].calls == [(1, {"outcome": "requeued"})]


def test_database_gauge_snapshot_reports_pending_and_oldest_queued_age() -> None:
    from app.telemetry import read_database_gauges

    session = Mock()
    session.scalar.side_effect = [4, 12.75]
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session

    snapshot = read_database_gauges(factory)

    assert snapshot == {"outbox_pending_jobs": 4, "agent_oldest_queued_age_seconds": 12.75}
