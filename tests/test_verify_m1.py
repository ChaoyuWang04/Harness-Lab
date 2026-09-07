from __future__ import annotations

import sys
import unittest
from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from scripts.verify_m1 import (  # noqa: E402
    evaluate_baseline,
    ordered_lifecycle_ok,
    resource_gate_ok,
    sse_replay_ok,
)


GOOD_EVENTS = [
    "run.created",
    "run.enqueued",
    "run.started",
    "step.model_call",
    "step.tool_call",
    "step.tool_result",
    "step.model_call",
    "run.completed",
]


class M1VerifierTests(unittest.TestCase):
    def test_ordered_lifecycle_requires_tool_result_and_terminal_order(self) -> None:
        self.assertTrue(ordered_lifecycle_ok(GOOD_EVENTS))
        self.assertFalse(ordered_lifecycle_ok([event for event in GOOD_EVENTS if event != "step.tool_result"]))
        self.assertFalse(ordered_lifecycle_ok(list(reversed(GOOD_EVENTS))))

    def test_baseline_allows_two_bad_outputs_but_no_system_failure(self) -> None:
        records = [
            {"status": "completed", "error_code": None, "post_ms": 20 + index, "events": GOOD_EVENTS}
            for index in range(18)
        ] + [
            {"status": "failed", "error_code": "BAD_OUTPUT", "post_ms": 30, "events": []},
            {"status": "failed", "error_code": "BAD_OUTPUT", "post_ms": 31, "events": []},
        ]
        result = evaluate_baseline(records)
        self.assertTrue(result["passed"])
        self.assertEqual(result["completed"], 18)

        records[-1]["error_code"] = "INTERNAL_ERROR"
        self.assertFalse(evaluate_baseline(records)["passed"])

    def test_post_p95_must_be_strictly_below_100_ms(self) -> None:
        records = [
            {"status": "completed", "error_code": None, "post_ms": 99, "events": GOOD_EVENTS}
            for _ in range(20)
        ]
        self.assertTrue(evaluate_baseline(records)["passed"])
        records[-1]["post_ms"] = 120
        self.assertFalse(evaluate_baseline(records)["passed"])

    def test_sse_replay_is_exact_contiguous_suffix(self) -> None:
        self.assertTrue(sse_replay_ok([1, 2, 3, 4, 5], last_event_id=2, replayed=[3, 4, 5]))
        self.assertFalse(sse_replay_ok([1, 2, 3, 4, 5], last_event_id=2, replayed=[3, 5]))
        self.assertFalse(sse_replay_ok([1, 2, 3], last_event_id=1, replayed=[2, 2, 3]))

    def test_resource_gate_is_four_gib_zero_oom_zero_restart(self) -> None:
        healthy = [
            {"memory_bytes": 512 * 1024**2, "oom_killed": False, "restart_count": 0},
            {"memory_bytes": 1024 * 1024**2, "oom_killed": False, "restart_count": 0},
        ]
        self.assertTrue(resource_gate_ok(healthy))
        self.assertFalse(resource_gate_ok(healthy + [{"memory_bytes": 3 * 1024**3, "oom_killed": False, "restart_count": 0}]))
        self.assertFalse(resource_gate_ok([{"memory_bytes": 1, "oom_killed": True, "restart_count": 0}]))
        self.assertFalse(resource_gate_ok([{"memory_bytes": 1, "oom_killed": False, "restart_count": 1}]))


if __name__ == "__main__":
    unittest.main()
