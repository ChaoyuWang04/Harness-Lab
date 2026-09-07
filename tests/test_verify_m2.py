from __future__ import annotations

import importlib.util
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


LAB_ROOT = Path(__file__).resolve().parents[1]
VERIFIER = LAB_ROOT / "scripts" / "verify_m2.py"


def load_verifier():
    spec = importlib.util.spec_from_file_location("verify_m2", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exact_cohort_requires_thirty_unique_terminal_runs() -> None:
    verifier = load_verifier()
    runs = [{"run_id": f"run_{index}", "status": "completed"} for index in range(30)]

    assert verifier.validate_cohort(runs)["ok"] is True
    with pytest.raises(AssertionError, match="exactly 30"):
        verifier.validate_cohort(runs[:-1])
    with pytest.raises(AssertionError, match="terminal"):
        verifier.validate_cohort([*runs[:-1], {"run_id": "run_x", "status": "running"}])


def test_gate_requires_four_panels_three_numbers_and_resources() -> None:
    verifier = load_verifier()
    panels = {
        "run_p95_seconds": 4.2,
        "failed_rate": 0.0,
        "queue_lag_p95_seconds": 0.8,
        "model_or_tool_series": 3,
        "run_samples": 30,
    }
    resources = {
        "runtime_host": "samwang-X870I-AORUS-PRO-ICE",
        "docker_mem_total_bytes": 33237381120,
        "running_services": [
            "api",
            "dispatcher",
            "lgtm",
            "ollama",
            "postgres",
            "redis",
            "sweeper",
            "worker",
        ],
        "aggregate_rss_mib": 1024,
        "oom_count": 0,
        "unexpected_restart_count": 0,
    }

    assert verifier.validate_panel_results(panels)["ok"] is True
    assert verifier.validate_resources(resources)["ok"] is True
    with pytest.raises(AssertionError, match="four dashboard"):
        verifier.validate_panel_results({**panels, "model_or_tool_series": 0})
    with pytest.raises(AssertionError, match="finite"):
        verifier.validate_panel_results({**panels, "run_p95_seconds": float("nan")})
    with pytest.raises(AssertionError, match="RSS"):
        verifier.validate_resources({**resources, "aggregate_rss_mib": 4096})
    with pytest.raises(AssertionError, match="running services"):
        verifier.validate_resources({**resources, "running_services": ["api"]})
    with pytest.raises(AssertionError, match="Docker memory"):
        verifier.validate_resources({**resources, "docker_mem_total_bytes": 0})


def test_written_gate_evidence_is_recursively_secret_free(tmp_path: Path) -> None:
    verifier = load_verifier()
    evidence = {"configured": {"sentry": True, "langfuse": True}, "run_ids": ["run_1"]}
    output = tmp_path / "gate.json"

    verifier.write_redacted_evidence(output, evidence, ["dsn-secret", "lf-secret"])

    assert json.loads(output.read_text()) == evidence
    with pytest.raises(AssertionError, match="secret"):
        verifier.write_redacted_evidence(output, {"bad": "dsn-secret"}, ["dsn-secret"])


def test_trace_requires_every_owned_async_and_agent_span() -> None:
    verifier = load_verifier()
    names = {
        "harness.api_create_run",
        "harness.dispatch",
        "harness.execute_run",
        "harness.model_call",
        "harness.tool_call",
    }

    assert verifier.validate_trace_spans(names)["ok"] is True
    with pytest.raises(AssertionError, match="missing required spans"):
        verifier.validate_trace_spans(names - {"harness.dispatch"})


def test_tempo_query_waits_for_eventual_indexing(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = load_verifier()
    searches = iter([{"traces": []}, {"traces": [{"traceID": "trace-1"}]}])
    trace = {
        "batches": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {"spanId": str(index), "name": name}
                            for index, name in enumerate(verifier.REQUIRED_TRACE_SPANS)
                        ]
                    }
                ]
            }
        ]
    }

    def fake_request(url: str, **_kwargs):
        return trace if "/api/traces/" in url else next(searches)

    sleeps: list[float] = []
    monkeypatch.setattr(verifier, "_json_request", fake_request)
    monkeypatch.setattr(verifier.time, "sleep", sleeps.append)

    result = verifier.query_tempo("http://grafana.invalid", "run-1", attempts=2, delay_seconds=0.1)

    assert result["ok"] is True
    assert sleeps == [0.1]


def test_langfuse_reader_uses_direct_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = load_verifier()
    captured: dict[str, object] = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=fake_client))

    verifier._langfuse_http_client()

    assert captured == {"timeout": 30, "trust_env": False}


def test_metric_queries_subtract_recorded_cohort_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = load_verifier()
    calls: list[tuple[str, str]] = []

    def fake_query(_base: str, expression: str, timestamp: str):
        calls.append((expression, timestamp))
        is_end = timestamp.endswith("01:31+00:00")
        instances = ["old", *[f"new-{index}" for index in range(30)]] if is_end else ["old"]
        if "_bucket" in expression:
            return [
                {
                    "metric": {"service_instance_id": instance, "le": le},
                    "value": [0, "1" if le in {"5", "+Inf"} else "0"],
                }
                for instance in instances
                for le in ("1", "5", "+Inf")
            ]
        if "agent_run_total" in expression:
            return [
                {
                    "metric": {"service_instance_id": instance, "status": "completed"},
                    "value": [0, "1"],
                }
                for instance in instances
            ]
        label = "http_status" if "model" in expression else "tool_name"
        return [
            {
                "metric": {"service_instance_id": instance, label: "200"},
                "value": [0, "2"],
            }
            for instance in instances
        ]

    monkeypatch.setattr(verifier, "prometheus_query", fake_query)
    monkeypatch.setattr(
        verifier,
        "_json_request",
        lambda *_args, **_kwargs: {"dashboard": {"panels": [{}, {}, {}, {}]}},
    )
    result = verifier.query_panels(
        "http://grafana.invalid",
        "2026-09-07T00:00:00+00:00",
        "2026-09-07T00:01:31+00:00",
        expected_run_count=30,
    )

    assert result["run_p95_seconds"] == pytest.approx(4.8)
    assert result["failed_rate"] == 0
    assert result["run_samples"] == 30
    assert result["model_or_tool_series"] == 2
    assert {timestamp for _, timestamp in calls} == {
        "2026-09-07T00:00:00+00:00",
        "2026-09-07T00:01:31+00:00",
    }
    assert all('service_name="harness-worker"' in expression for expression, _ in calls)
