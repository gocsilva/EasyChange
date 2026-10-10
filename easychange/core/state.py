from __future__ import annotations

import json
import os
import secrets
import tempfile
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
                if isinstance(loaded, dict) and loaded.get("workspace") == workspace:
                    self.data.update(loaded)
            except (json.JSONDecodeError, OSError):
                pass
        path.parent.mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        payload = json.dumps(
            self.data, ensure_ascii=False, separators=(",", ":")
        )
        last_error: OSError | None = None
        for _attempt in range(2):
            temporary: Path | None = None
            descriptor: int | None = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Parallel EasyChange instances must never share a .tmp name:
                # each write gets its own staging file on the same volume.
                descriptor, name = tempfile.mkstemp(
                    prefix=f".{self.path.name}.", suffix=".tmp",
                    dir=self.path.parent)
                temporary = Path(name)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    descriptor = None
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                if os.name == "posix":
                    # fsync(file) persists bytes, but not necessarily the new
                    # directory entry installed by atomic rename on power loss.
                    # Filesystems lacking directory fsync retain atomic replace.
                    directory_fd = None
                    try:
                        directory_fd = os.open(
                            self.path.parent,
                            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                        os.fsync(directory_fd)
                    except OSError:
                        pass
                    finally:
                        if directory_fd is not None:
                            os.close(directory_fd)
                return
            except FileNotFoundError as exc:
                last_error = exc
                continue
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        if last_error is not None:
            raise last_error
