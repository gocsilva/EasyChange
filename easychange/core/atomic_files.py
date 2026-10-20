"""Bounded retry helpers for cross-platform atomic file replacement."""
from __future__ import annotations

import os
import time
from typing import Any


def replace_with_retry(source: Any, destination: Any) -> None:
    """Retry brief Windows sharing violations without hiding permanent errors."""
    for attempt in range(4):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if (os.name != "nt" or winerror not in {5, 32, 33}
                    or attempt == 3):
                raise
            time.sleep(0.01 * (2 ** attempt))
