from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


@dataclass(slots=True)
class JournalEntry:
    change_id: str
    session_id: str
    timestamp: str
    command: str
    path: str
    before: str | None
    after: str | None
    before_hash: str | None
    after_hash: str | None
    transaction_id: str | None = None
    source: str = "command"


def content_hash(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Journal:
    """Append-only, workspace-local edit journal. Entries retain content for reliable undo."""
    def __init__(self, path: Path, session_id: str) -> None:
        self.path = path
        self.session_id = session_id
        self._lock = threading.RLock()
        self._entries: list[JournalEntry] = []
        self._redo: list[JournalEntry] = []
        self._counter = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                try:
                    value = json.loads(line)
                    if value.get("type") == "meta":
                        self._counter = max(self._counter, int(value.get("counter", 0)))
                        self._redo = [JournalEntry(**item) for item in value.get("redo", [])]
                        continue
                    entry = JournalEntry(**value)
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue
                self._entries.append(entry)
                self._counter = max(self._counter, int(entry.change_id.removeprefix("CH")))

    @property
    def entries(self) -> list[JournalEntry]:
        return list(self._entries)

    @property
    def redo_entries(self) -> list[JournalEntry]:
        return list(self._redo)

    def record(self, command: str, path: str, before: str | None, after: str | None,
               transaction_id: str | None = None, source: str = "command") -> JournalEntry:
        with self._lock:
            self._counter += 1
            entry = JournalEntry(f"CH{self._counter}", self.session_id,
                datetime.now(timezone.utc).isoformat(), command, path, before, after,
                content_hash(before), content_hash(after), transaction_id, source)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            self._entries.append(entry)
            self._redo.clear()
            return entry

    def undo(self, change_id: str | None = None) -> list[JournalEntry]:
        with self._lock:
            if not self._entries:
                return []
            if change_id:
                index = next((i for i, entry in enumerate(self._entries) if entry.change_id == change_id), None)
                if index is None:
                    raise LookupError(f"Journal entry not found: {change_id}")
                entry = self._entries.pop(index)
                if any(item.path == entry.path for item in self._entries[index:]):
                    self._entries.insert(index, entry)
                    raise RuntimeError("Cannot undo this change before undoing later changes to the same file")
            else:
                entry = self._entries.pop()
            self._redo.append(entry)
            self._rewrite()
            return [entry]

    def redo(self) -> list[JournalEntry]:
        with self._lock:
            if not self._redo:
                return []
            entry = self._redo.pop()
            self._entries.append(entry)
            self._rewrite()
            return [entry]

    def clear_redo(self) -> None:
        self._redo.clear()

    def _rewrite(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"type": "meta", "counter": self._counter,
                                     "redo": [asdict(entry) for entry in self._redo]}, ensure_ascii=False) + "\n")
            for entry in self._entries:
                stream.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        temporary.replace(self.path)
