"""User-requested, process-local, bounded BLE/GATT diagnostic trace.

Only connection metadata and exception stack locations belong here.
Never record commands, QR results, pairing codes, keys or BLE payload bytes.
The remote Windows user explicitly chooses whether to copy/save these logs.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
import re
import threading
import traceback


_SENSITIVE = re.compile(
    r"(?i)(?:\bBearer\s+)[A-Za-z0-9._~+/-]{5,}|"
    r"(?i:\b(?:token|password|secret|api[_-]?key|authorization|sas|"
    r"passkey|private[_-]?key|pair(?:ing)?[_-]?code)\s*[:=]\s*)[^\s,;]+"
)


def _safe_text(message: str) -> str:
    text = str(message)[:5500]
    def hide(match):
        value = match.group(0)
        if value.lower().startswith("bearer"):
            return "Bearer [REDACTED]"
        return value.split(":", 1)[0].split("=", 1)[0] + "=[REDACTED]"
    return _SENSITIVE.sub(hide, text)


class GattDiagnosticLog:
    """Thread-safe log snapshot; Qt reads this without touching BLE thread."""

    def __init__(self, max_events: int = 750) -> None:
        self._events = deque(maxlen=max(60, min(1500, int(max_events))))
        self._mutex = threading.RLock()
        self._sequence = 0

    def record(self, stage: str, message: str, *, exc: BaseException | None = None) -> None:
        stage = re.sub(r"[^A-Z0-9_/-]", "_", str(stage).upper())[:38]
        summary = _safe_text(message)
        if exc is not None:
            # No captured local variables or sensitive command/pairing material.
            trace = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__, limit=12))
            summary += "\n" + _safe_text(trace[:5500])
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        with self._mutex:
            self._sequence += 1
            self._events.append({
                "id": self._sequence, "timestamp": stamp,
                "stage": stage, "message": summary[:8000],
            })

    def snapshot(self, after: int = 0, limit: int = 160) -> dict:
        try:
            after = max(0, int(after))
            limit = max(1, min(300, int(limit)))
        except (ValueError, TypeError, OverflowError):
            after, limit = 0, 160
        with self._mutex:
            results = [dict(row) for row in self._events if row["id"] > after][:limit]
            oldest = self._events[0]["id"] if self._events else 0
            cursor = results[-1]["id"] if results else after
            return {
                "events": results,
                "cursor": cursor,
                "oldest": oldest,
                "dropped": max(0, oldest - after - 1) if after else 0,
            }
