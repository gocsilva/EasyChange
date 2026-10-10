from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


_BLOB_THRESHOLD_BYTES = 4096
_BLOB_PREFIX = "@zblob:"
_MAX_BLOB_COMPRESSED_BYTES = 64 * 1024 * 1024
_MAX_BLOB_DECOMPRESSED_BYTES = 128 * 1024 * 1024
_MAX_JOURNAL_RECORD_CHARS = 16 * 1024 * 1024


def _sync_directory_best_effort(folder: Path) -> None:
    """Persist a newly published journal/blob name where POSIX permits."""
    if os.name != "posix":
        return
    descriptor = None
    try:
        descriptor = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
    except OSError:
        # Some filesystems do not support fsync() on directories. The file
        # itself was already synced and its rename remains atomic.
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _bounded_journal_jsonl(value: dict) -> str:
    """Never append a JSONL line that recovery will reject as oversized."""
    encoded = []
    chars = 1  # terminating newline counts toward readline()'s limit
    writer = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    for fragment in writer.iterencode(value):
        chars += len(fragment)
        if chars > _MAX_JOURNAL_RECORD_CHARS:
            raise ValueError("JOURNAL_RECORD_TOO_LARGE")
        encoded.append(fragment)
    return "".join(encoded) + "\n"


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
    """Append-only edit journal with lazy content-addressed snapshots.

    Large before/after values remain compressed on disk and are materialized
    only when a caller actually needs them.
    """

    def __init__(self, path: Path, session_id: str) -> None:
        self.path = path
        self.session_id = session_id
        self.blob_root = path.parent / "blobs"
        self._lock = threading.RLock()
        self._entries: list[JournalEntry] = []
        self._redo: list[JournalEntry] = []
        self._counter = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_root.mkdir(parents=True, exist_ok=True)

        if path.exists():
            try:
                stream = path.open("r", encoding="utf-8")
            except OSError:
                stream = None
            if stream is not None:
                with stream:
                    # Bound each JSONL entry before parsing. An oversized or
                    # truncated record must not consume unbounded memory or
                    # silently erase journal truth from the recovered view.
                    while True:
                        line = stream.readline(_MAX_JOURNAL_RECORD_CHARS + 1)
                        if not line:
                            break
                        if len(line) > _MAX_JOURNAL_RECORD_CHARS:
                            raise ValueError("JOURNAL_RECORD_TOO_LARGE")
                        try:
                            value = json.loads(line)
                            if not isinstance(value, dict):
                                raise ValueError("JOURNAL_RECORD_NOT_OBJECT")
                            if value.get("type") == "meta":
                                self._counter = max(self._counter, int(value.get("counter", 0)))
                                self._redo = [
                                    self._compact_from_serialized(item)
                                    for item in value.get("redo", [])
                                    if isinstance(item, dict)
                                ]
                                continue
                            entry = self._compact_from_serialized(value)
                        except (json.JSONDecodeError, TypeError, KeyError, OSError) as exc:
                            # Bad legacy entries are kept on disk for later
                            # analysis; no entry can be discarded because of
                            # an untrusted payload's massive size.
                            continue
                        except ValueError as exc:
                            if str(exc) == "JOURNAL_RECORD_NOT_OBJECT":
                                raise
                            continue
                        self._entries.append(entry)
                        try:
                            self._counter = max(self._counter, int(entry.change_id.removeprefix("CH")))
                        except ValueError:
                            pass

    def _store_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith(_BLOB_PREFIX):
            digest = value[len(_BLOB_PREFIX):]
            if re_full_hash(digest):
                return value
        raw = value.encode("utf-8")
        if len(raw) < _BLOB_THRESHOLD_BYTES:
            return value
        if len(raw) > _MAX_BLOB_DECOMPRESSED_BYTES:
            raise ValueError("JOURNAL_BLOB_DECOMPRESSED_TOO_LARGE")
        digest = hashlib.sha256(raw).hexdigest()
        target = self.blob_root / f"{digest}.zlib"
        if not target.exists():
            compressed = zlib.compress(raw, level=6)
            if len(compressed) > _MAX_BLOB_COMPRESSED_BYTES:
                raise ValueError("JOURNAL_BLOB_COMPRESSED_TOO_LARGE")
            # Multiple writers can store the same digest simultaneously.
            # Per-writer staging files prevent clobbered temporary snapshots.
            fd, name = tempfile.mkstemp(
                prefix=target.name + ".", suffix=".tmp", dir=self.blob_root)
            temporary = Path(name)
            try:
                try:
                    stream = os.fdopen(fd, "wb")
                except BaseException:
                    # mkstemp owns the raw descriptor until fdopen succeeds.
                    os.close(fd)
                    raise
                with stream:
                    stream.write(compressed)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                _sync_directory_best_effort(self.blob_root)
            finally:
                temporary.unlink(missing_ok=True)
        return _BLOB_PREFIX + digest

    def _load_text(self, value: str | None) -> str | None:
        if value is None or not value.startswith(_BLOB_PREFIX):
            return value
        digest = value[len(_BLOB_PREFIX):]
        if not re_full_hash(digest):
            raise ValueError("Invalid journal blob reference")
        target = self.blob_root / f"{digest}.zlib"
        with target.open("rb") as stream:
            compressed = stream.read(_MAX_BLOB_COMPRESSED_BYTES + 1)
        if len(compressed) > _MAX_BLOB_COMPRESSED_BYTES:
            raise ValueError("JOURNAL_BLOB_COMPRESSED_TOO_LARGE")
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, _MAX_BLOB_DECOMPRESSED_BYTES + 1)
        if len(raw) > _MAX_BLOB_DECOMPRESSED_BYTES or decoder.unconsumed_tail:
            raise ValueError("JOURNAL_BLOB_DECOMPRESSED_TOO_LARGE")
        raw += decoder.flush(_MAX_BLOB_DECOMPRESSED_BYTES + 1 - len(raw))
        if len(raw) > _MAX_BLOB_DECOMPRESSED_BYTES:
            raise ValueError("JOURNAL_BLOB_DECOMPRESSED_TOO_LARGE")
        if not decoder.eof or decoder.unused_data:
            raise ValueError("JOURNAL_BLOB_INVALID_STREAM")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Journal blob hash mismatch")
        return raw.decode("utf-8")

    def _serialize_entry(self, entry: JournalEntry) -> dict:
        value = asdict(entry)
        value["before"] = self._store_text(entry.before)
        value["after"] = self._store_text(entry.after)
        return value

    @staticmethod
    def _compact_from_serialized(value: dict) -> JournalEntry:
        return JournalEntry(**dict(value))

    def _materialize_entry(self, entry: JournalEntry) -> JournalEntry:
        data = asdict(entry)
        data["before"] = self._load_text(data.get("before"))
        data["after"] = self._load_text(data.get("after"))
        return JournalEntry(**data)

    @property
    def entries(self) -> list[JournalEntry]:
        with self._lock:
            return [self._materialize_entry(entry) for entry in self._entries]

    @property
    def redo_entries(self) -> list[JournalEntry]:
        with self._lock:
            return [self._materialize_entry(entry) for entry in self._redo]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def metadata(self, offset: int = 0, limit: int = 50) -> list[dict]:
        with self._lock:
            start = max(0, int(offset))
            selected = self._entries[start:start + max(1, int(limit))]
            return [{
                "change_id": entry.change_id,
                "session_id": entry.session_id,
                "timestamp": entry.timestamp,
                "command": entry.command,
                "path": entry.path,
                "before_hash": entry.before_hash,
                "after_hash": entry.after_hash,
                "transaction_id": entry.transaction_id,
                "source": entry.source,
            } for entry in selected]

    def get(self, change_id: str | None = None) -> JournalEntry | None:
        with self._lock:
            if not self._entries:
                return None
            compact = (
                next((entry for entry in self._entries if entry.change_id == change_id), None)
                if change_id else self._entries[-1]
            )
            return self._materialize_entry(compact) if compact is not None else None

    def slice(self, start: int, stop: int | None = None) -> list[JournalEntry]:
        with self._lock:
            selected = self._entries[max(0, int(start)):stop]
            return [self._materialize_entry(entry) for entry in selected]

    def record(self, command: str, path: str, before: str | None, after: str | None,
               transaction_id: str | None = None, source: str = "command") -> JournalEntry:
        with self._lock:
            next_counter = self._counter + 1
            entry = JournalEntry(
                f"CH{next_counter}",
                self.session_id,
                datetime.now(timezone.utc).isoformat(),
                command,
                path,
                before,
                after,
                content_hash(before),
                content_hash(after),
                transaction_id,
                source,
            )
            serialized = self._serialize_entry(entry)
            line = _bounded_journal_jsonl(serialized)
            was_missing = not self.path.exists()
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            if was_missing:
                _sync_directory_best_effort(self.path.parent)
            self._counter = next_counter
            self._entries.append(self._compact_from_serialized(serialized))
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
                compact = self._entries.pop(index)
                if any(item.path == compact.path for item in self._entries[index:]):
                    self._entries.insert(index, compact)
                    raise RuntimeError("Cannot undo this change before undoing later changes to the same file")
            else:
                compact = self._entries.pop()
            self._redo.append(compact)
            self._rewrite()
            return [self._materialize_entry(compact)]

    def redo(self) -> list[JournalEntry]:
        with self._lock:
            if not self._redo:
                return []
            compact = self._redo.pop()
            self._entries.append(compact)
            self._rewrite()
            return [self._materialize_entry(compact)]

    def clear_redo(self) -> None:
        self._redo.clear()

    def referenced_blobs(self) -> set[str]:
        with self._lock:
            output: set[str] = set()
            for entry in [*self._entries, *self._redo]:
                for value in (entry.before, entry.after):
                    if isinstance(value, str) and value.startswith(_BLOB_PREFIX):
                        digest = value[len(_BLOB_PREFIX):]
                        if re_full_hash(digest):
                            output.add(digest)
            return output

    def _rewrite(self) -> None:
        # Undo/redo rewrites must never share a temporary filename across
        # concurrent EasyChange instances. Rewriting stays atomic on disk.
        fd, name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temporary = Path(name)
        try:
            try:
                stream = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
            except BaseException:
                os.close(fd)
                raise
            with stream:
                stream.write(_bounded_journal_jsonl(
                    {"type": "meta", "counter": self._counter,
                     "redo": [self._serialize_entry(entry) for entry in self._redo]}))
                for entry in self._entries:
                    stream.write(_bounded_journal_jsonl(
                        self._serialize_entry(entry)))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            _sync_directory_best_effort(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)

def re_full_hash(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
