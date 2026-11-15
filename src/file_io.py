"""Small shared-file helpers for atomic JSON/status handoffs.

The LSE bridge writes a heartbeat with a temporary file + ``os.replace``.  On
Windows, a reader can briefly see an antivirus/indexer lock; retrying once or
twice is preferable to declaring the live bridge dead.
"""

from __future__ import annotations

import time
from pathlib import Path


def read_text_shared(
    path: str | Path,
    *,
    attempts: int = 5,
    delay_seconds: float = 0.02,
) -> str:
    """Read a small shared file, retrying transient Windows sharing errors."""
    target = Path(path)
    last: OSError | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            return target.read_text(encoding="utf-8")
        except OSError as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(max(0.0, float(delay_seconds)))
    raise last or OSError(f"could not read {target}")
