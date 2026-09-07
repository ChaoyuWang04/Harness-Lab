from __future__ import annotations

import importlib.util
from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]
VERIFIER = LAB_ROOT / "scripts" / "verify_alert.py"


def load_verifier():
    spec = importlib.util.spec_from_file_location("verify_alert", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_queue_alert_filter_only_accepts_active_target_rule() -> None:
    verifier = load_verifier()
    alerts = [
        {"labels": {"alertname": "Harness failed rate"}, "status": {"state": "active"}},
        {"labels": {"alertname": "Harness queue lag"}, "status": {"state": "suppressed"}},
        {"labels": {"alertname": "Harness queue lag"}, "status": {"state": "active"}},
    ]

    assert verifier.active_queue_alerts(alerts) == [alerts[-1]]


def test_alert_evidence_requires_negative_firing_recovery_and_terminal_run() -> None:
    verifier = load_verifier()
    evidence = {
        "negative": {"duration_seconds": 120, "max_value": 0, "active_alerts": 0},
        "positive": {"max_value": 131, "firing": True},
        "recovery": {"active_alerts": 0, "run_status": "completed"},
    }

    assert verifier.validate_evidence(evidence)["ok"] is True
