from __future__ import annotations

import threading
from collections.abc import Callable

from .indexer import Indexer


class WorkspaceWatcher:
    """Polling watcher that refreshes only files whose stat metadata changed."""
    def __init__(self, indexer: Indexer, interval: float = 1.5,
                 on_change: Callable[[dict], None] | None = None) -> None:
        self.indexer = indexer
        self.interval = max(0.2, interval)
        self.on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_change: dict | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="easychange-watcher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread: self._thread.join(timeout)

    def poll_once(self) -> dict:
        # Watch mode is explicit and correctness-first: force metadata refresh.
        # Normal AI commands keep the hot index O(1) and use update_path() for
        # EasyChange-owned mutations.
        result = self.indexer.refresh(force=True)
        if result["indexed"] or result["removed"]:
            self.last_change = result
            if self.on_change: self.on_change(result)
        return result

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll_once()
                self.last_error = None
            except (OSError, RuntimeError) as exc:
                self.last_error = str(exc)
