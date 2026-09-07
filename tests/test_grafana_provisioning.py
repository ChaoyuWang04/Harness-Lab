from __future__ import annotations

import json
from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]
GRAFANA = LAB_ROOT / "config" / "grafana"


def test_dashboard_has_four_slo_panels_with_thresholds_and_stable_datasource() -> None:
    dashboard = json.loads((GRAFANA / "dashboards" / "harness-slo.json").read_text(encoding="utf-8"))

    assert dashboard["uid"] == "harness-slo"
    panels = dashboard["panels"]
    assert [panel["title"] for panel in panels] == [
        "Run P95 duration",
        "Run failed rate",
        "Queue lag P95",
        "Model errors and tool latency",
    ]
    assert all(panel["datasource"]["uid"] == "prometheus" for panel in panels)
    assert "60" in json.dumps(panels[0]["fieldConfig"])
    assert "0.01" in json.dumps(panels[1]["fieldConfig"])
    assert "10" in json.dumps(panels[2]["fieldConfig"])
    expressions = "\n".join(target["expr"] for panel in panels for target in panel["targets"])
    for metric in (
        "agent_run_duration_seconds_bucket",
        "agent_run_failed_total",
        "agent_queue_lag_seconds_bucket",
        "agent_model_call_total",
        "agent_tool_latency_ms_bucket",
    ):
        assert metric in expressions


def test_dashboard_queries_support_one_sample_rq_workhorses() -> None:
    dashboard = json.loads((GRAFANA / "dashboards" / "harness-slo.json").read_text(encoding="utf-8"))
    panels = {panel["title"]: panel for panel in dashboard["panels"]}

    duration_query = panels["Run P95 duration"]["targets"][0]["expr"]
    queue_query = panels["Queue lag P95"]["targets"][0]["expr"]
    failed_query = panels["Run failed rate"]["targets"][0]["expr"]

    # Every RQ workhorse exports one cumulative histogram sample before exit.
    # rate() cannot calculate from those one-sample series; aggregate the
    # collector-retained cumulative buckets instead.
    assert "rate(" not in duration_query
    assert "rate(" not in queue_query
    assert "sum by (le) (agent_run_duration_seconds_bucket)" in duration_query
    assert "sum by (le) (agent_queue_lag_seconds_bucket)" in queue_query
    assert "or vector(0)" in failed_query


def test_model_errors_and_tool_latency_use_separate_axes() -> None:
    dashboard = json.loads((GRAFANA / "dashboards" / "harness-slo.json").read_text(encoding="utf-8"))
    panel = next(panel for panel in dashboard["panels"] if panel["title"] == "Model errors and tool latency")

    assert panel["fieldConfig"]["overrides"]
    axis_units = json.dumps(panel["fieldConfig"]["overrides"])
    assert "short" in axis_units
    assert "ms" in axis_units
    assert "axisPlacement" in axis_units


def test_datasources_and_dashboard_provider_use_lgtm_stable_uids() -> None:
    datasources = (GRAFANA / "provisioning" / "datasources" / "datasources.yaml").read_text()
    provider = (GRAFANA / "provisioning" / "dashboards" / "dashboards.yaml").read_text()

    assert "uid: prometheus" in datasources
    assert "uid: tempo" in datasources
    assert "uid: loki" in datasources
    assert "http://127.0.0.1:9090" in datasources
    assert "/otel-lgtm/grafana/conf/provisioning/dashboards/custom" in provider


def test_alerts_have_exact_queue_expression_and_two_minute_pending_period() -> None:
    rules = (GRAFANA / "provisioning" / "alerting" / "rules.yaml").read_text()

    assert "max(agent_oldest_queued_age_seconds) > 10" in rules
    assert "failed_rate > 0.05" in rules
    assert rules.count("for: 2m") == 2
    assert "datasourceUid: prometheus" in rules


def test_compose_mounts_only_lab_grafana_files_read_only() -> None:
    compose = (LAB_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "./config/grafana/provisioning/datasources/datasources.yaml:" in compose
    assert "./config/grafana/provisioning/dashboards/dashboards.yaml:" in compose
    assert "./config/grafana/provisioning/alerting/rules.yaml:" in compose
    assert "./config/grafana/dashboards/harness-slo.json:" in compose
    assert "/otel-lgtm/grafana/conf/provisioning/dashboards/harness.yaml:ro" in compose
    assert compose.count(":ro") >= 4
