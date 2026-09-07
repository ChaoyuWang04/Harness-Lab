from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))


def test_debug_metrics_return_only_aggregate_counts() -> None:
    from app.api.routes_metrics import collect_debug_metrics

    session = Mock()
    session.scalar.side_effect = [3, 2, 4, 5, 6, 7]

    result = collect_debug_metrics(session)

    assert result == {
        "outbox_pending": 3,
        "runs": {
            "queued": 2,
            "running": 4,
            "completed": 5,
            "failed": 6,
            "cancelled": 7,
        },
    }
    assert "run_id" not in repr(result)
