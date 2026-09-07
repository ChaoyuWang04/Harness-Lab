from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


def crash_after_publish_once(
    marker: Path,
    *,
    exit_process: Callable[[int], object] = os._exit,
) -> bool:
    """Exit exactly once after atomically claiming a durable marker."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(datetime.now(timezone.utc).isoformat() + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    exit_process(91)
    return True
