from __future__ import annotations

import inspect
import sys
from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.chaos.hooks import crash_after_publish_once  # noqa: E402
from app.outbox import dispatch_batch  # noqa: E402


def test_dispatch_batch_exposes_post_publish_hook() -> None:
    assert "post_publish_hook" in inspect.signature(dispatch_batch).parameters


def test_dispatcher_crash_marker_is_atomic_and_one_shot(tmp_path: Path) -> None:
    marker = tmp_path / "dispatcher-after-publish.once"
    exits: list[int] = []

    first = crash_after_publish_once(marker, exit_process=exits.append)
    second = crash_after_publish_once(marker, exit_process=exits.append)

    assert first is True
    assert second is False
    assert exits == [91]
    assert marker.read_text(encoding="utf-8").strip()
