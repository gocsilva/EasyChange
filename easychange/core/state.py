from __future__ import annotations

import json
import os
import secrets
from pathlib import Path


class MachineSession:
    """Small persisted view/session record; persisted atomically under .easychange."""
    def __init__(self, path: Path, workspace: str) -> None:
        self.path = path
        self.data = {"session_id": "S" + secrets.token_hex(3).upper(), "workspace": workspace,
                     "current_file": None, "line": 1, "output_mode": "text", "transport": "local",
                     "history": [], "aliases": {"o": "open", "r": "read", "s": "search", "b": "build", "t": "test", "d": "diff"}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if loaded.get("workspace") == workspace:
                    self.data.update(loaded)
            except (json.JSONDecodeError, OSError):
                pass
        path.parent.mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        payload = json.dumps(
            self.data, ensure_ascii=False, separators=(",", ":")
        )
        temp = self.path.with_suffix(".tmp")
        last_error: OSError | None = None
        for _attempt in range(2):
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with temp.open("w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.path)
                return
            except FileNotFoundError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
