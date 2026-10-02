from __future__ import annotations
import os

import difflib
import hashlib
import mimetypes
import threading
from collections import OrderedDict
from pathlib import Path

from .workspace import Workspace

MAX_TEXT_FILE_BYTES = 4 * 1024 * 1024


class ExternalChangeError(RuntimeError):
    def __init__(self, path: str, expected: str, actual: str) -> None:
        super().__init__(f"File changed outside EasyChange: {path}")
        self.path = path
        self.expected = expected
        self.actual = actual


class FileService:
    def __init__(self, workspace: Workspace, baselines: dict[str, str] | None = None) -> None:
        self.workspace = workspace
        self._opened: dict[str, Path] = {}
        self._baselines: dict[str, str] = dict(baselines or {})
        self._guard = threading.RLock()
        # Read-only hot cache. Mutation paths deliberately bypass this cache and
        # hash the actual file again so external-change protection is unchanged.
        self._read_cache: OrderedDict[str, tuple[int, int, str, list[str]]] = OrderedDict()
        self._read_cache_bytes = 0
        self._read_cache_max_entries = 32
        self._read_cache_max_bytes = 16 * 1024 * 1024

    @property
    def baselines(self) -> dict[str, str]:
        with self._guard: return dict(self._baselines)

    def _read_cache_get(self, relative: str, size: int, mtime_ns: int):
        with self._guard:
            cached = self._read_cache.get(relative)
            if cached is None:
                return None
            cached_size, cached_mtime, digest, lines = cached
            if cached_size != size or cached_mtime != mtime_ns:
                self._read_cache.pop(relative, None)
                self._read_cache_bytes = max(0, self._read_cache_bytes - cached_size)
                return None
            self._read_cache.move_to_end(relative)
            return digest, lines

    def _read_cache_put(self, relative: str, size: int, mtime_ns: int,
                        digest: str, lines: list[str]) -> None:
        if size > self._read_cache_max_bytes:
            return
        with self._guard:
            previous = self._read_cache.pop(relative, None)
            if previous is not None:
                self._read_cache_bytes = max(0, self._read_cache_bytes - previous[0])
            self._read_cache[relative] = (size, mtime_ns, digest, lines)
            self._read_cache_bytes += size
            while (
                len(self._read_cache) > self._read_cache_max_entries
                or self._read_cache_bytes > self._read_cache_max_bytes
            ):
                _, stale = self._read_cache.popitem(last=False)
                self._read_cache_bytes = max(0, self._read_cache_bytes - stale[0])

    def _read_cache_drop(self, relative: str) -> None:
        with self._guard:
            previous = self._read_cache.pop(relative, None)
            if previous is not None:
                self._read_cache_bytes = max(0, self._read_cache_bytes - previous[0])

    def list_files(self, limit: int = 500) -> list[dict[str, str]]:
        items = []
        ignored = {".git", ".easychange", ".venv", "venv", "node_modules", "bin", "obj", "dist", "build",
                   "__pycache__", ".gradle", ".idea", ".vs", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
        for path in sorted(self.workspace.root_path.rglob("*")):
            if any(part in ignored for part in path.parts):
                continue
            if path.is_file():
                rel = path.relative_to(self.workspace.root_path).as_posix()
                items.append({"path": rel, "id": f"F{len(items) + 1}", "kind": "file"})
                if len(items) >= limit:
                    break
        return items

    def read(self, path: str, start: int = 1, count: int = 120) -> dict:
        target = self.workspace.resolve(path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(str(target))
        relative = target.relative_to(self.workspace.root_path).as_posix()
        stat = target.stat()
        if stat.st_size > MAX_TEXT_FILE_BYTES:
            raise ValueError(f"File exceeds text read limit ({MAX_TEXT_FILE_BYTES} bytes)")
        cached = self._read_cache_get(relative, stat.st_size, stat.st_mtime_ns)
        cache_hit = cached is not None
        if cached is None:
            data = target.read_bytes()
            if b"\0" in data:
                raise ValueError("Binary files cannot be read as text")
            if len(data) > MAX_TEXT_FILE_BYTES:
                raise ValueError(f"File exceeds text read limit ({MAX_TEXT_FILE_BYTES} bytes)")
            lines = data.decode("utf-8-sig").splitlines()
            digest = hashlib.sha256(data).hexdigest()
            # Use the post-read stat for cache identity. Atomic external replaces
            # between the first stat and read cannot poison a future cache hit.
            post = target.stat()
            self._read_cache_put(relative, post.st_size, post.st_mtime_ns, digest, lines)
        else:
            digest, lines = cached
        with self._guard:
            self._baselines[relative] = digest
        start = max(1, start)
        chunk = lines[start - 1:start - 1 + max(1, count)]
        return {"path": relative, "start": start,
                "total_lines": len(lines), "hash": digest[:12],
                "cache_hit": cache_hit,
                "lines": [f"{i}|{line}" for i, line in enumerate(chunk, start)]}

    def _read_for_edit(self, path: str) -> tuple[Path, str, str]:
        """Read one editable text snapshot and establish/verify its baseline."""
        target = self.workspace.resolve(path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(str(target))
        data = target.read_bytes()
        if b"\0" in data:
            raise ValueError("Binary files cannot be edited as text")
        if len(data) > MAX_TEXT_FILE_BYTES:
            raise ValueError(f"File exceeds text write limit ({MAX_TEXT_FILE_BYTES} bytes)")
        relative = target.relative_to(self.workspace.root_path).as_posix()
        actual_hash = hashlib.sha256(data).hexdigest()
        with self._guard:
            expected_hash = self._baselines.get(relative)
            if expected_hash and actual_hash != expected_hash:
                raise ExternalChangeError(relative, expected_hash, actual_hash)
            # write() will re-read immediately before replacing the file and
            # compare against this exact snapshot, preserving external-change
            # detection while avoiding CommandService pre/post reads.
            self._baselines[relative] = actual_hash
        return target, relative, data.decode("utf-8")
    def write(self, path: str, content: str, *, force: bool = False) -> dict:
        target = self.workspace.resolve(path)
        relative = target.relative_to(self.workspace.root_path).as_posix()
        if target.exists() and not target.is_file():
            raise IsADirectoryError(str(target))

        created = not target.exists()
        previous_bytes = b""
        before = ""
        before_hash = None
        if not created:
            previous_bytes = target.read_bytes()
            if b"\0" in previous_bytes:
                raise ValueError("Binary files cannot be overwritten as text")
            if len(previous_bytes) > MAX_TEXT_FILE_BYTES:
                raise ValueError(f"File exceeds text write limit ({MAX_TEXT_FILE_BYTES} bytes)")
            actual_hash = hashlib.sha256(previous_bytes).hexdigest()
            before_hash = actual_hash
            with self._guard:
                expected_hash = self._baselines.get(relative)
            if expected_hash and actual_hash != expected_hash and not force:
                raise ExternalChangeError(relative, expected_hash, actual_hash)
            before = previous_bytes.decode("utf-8")

        new_bytes = content.encode("utf-8")
        if len(new_bytes) > MAX_TEXT_FILE_BYTES:
            raise ValueError(f"File exceeds text write limit ({MAX_TEXT_FILE_BYTES} bytes)")
        expected_after_hash = hashlib.sha256(new_bytes).hexdigest()

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.easychange-{threading.get_ident()}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(new_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

        # Do not make the caller issue a read-after-write just to know whether
        # the mutation committed. Verify the exact bytes before returning.
        actual_bytes = target.read_bytes()
        actual_after_hash = hashlib.sha256(actual_bytes).hexdigest()
        if actual_after_hash != expected_after_hash:
            # Best-effort restoration protects against an exotic short/corrupt
            # local write even outside a higher-level transaction.
            if created:
                target.unlink(missing_ok=True)
            else:
                recovery = target.with_name(f".{target.name}.easychange-recovery.tmp")
                recovery.write_bytes(previous_bytes)
                recovery.replace(target)
            raise IOError("POST_WRITE_HASH_MISMATCH")

        self._opened[path] = target
        with self._guard:
            self._baselines[relative] = actual_after_hash
        written_stat = target.stat()
        self._read_cache_put(
            relative, written_stat.st_size, written_stat.st_mtime_ns,
            actual_after_hash, content.splitlines(),
        )

        return {
            "path": relative,
            "created": created,
            "before_hash": before_hash,
            "after_hash": actual_after_hash,
            "bytes_written": len(new_bytes),
            "verified": True,
            "diff": "".join(difflib.unified_diff(
                before.splitlines(True), content.splitlines(True),
                fromfile="before", tofile="after"
            )),
        }

    def replace(self, path: str, old: str, new: str, count: int = 0) -> dict:
        _, relative, original = self._read_for_edit(path)
        occurrences = original.count(old)
        if not occurrences:
            raise LookupError("Search text was not found")
        updated = original.replace(old, new, count) if count else original.replace(old, new)
        self.write(path, updated)
        return {
            "path": relative,
            "replacements": min(occurrences, count) if count else occurrences,
            "_before": original,
            "_after": updated,
        }

    def replace_line(self, path: str, line: int, value: str) -> dict:
        _, relative, original = self._read_for_edit(path)
        lines = original.splitlines(keepends=True)
        if line < 1 or line > len(lines):
            raise IndexError(f"Line out of range: {line}")
        ending = "\n" if lines[line - 1].endswith("\n") else ""
        lines[line - 1] = value + ending
        updated = "".join(lines)
        self.write(path, updated)
        return {"path": relative, "line": line, "_before": original, "_after": updated}

    def append(self, path: str, value: str) -> dict:
        target = self.workspace.resolve(path)
        before = target.read_text(encoding="utf-8") if target.exists() else ""
        separator = "" if not before or before.endswith(("\n", "\r")) else "\n"
        self.write(path, before + separator + value)
        return {"path": path, "appended_chars": len(value)}

    def insert_line(self, path: str, line: int, value: str) -> dict:
        _, relative, original = self._read_for_edit(path)
        lines = original.splitlines()
        if line < 1 or line > len(lines) + 1:
            raise IndexError(f"Line out of range: {line}")
        lines.insert(line - 1, value)
        ending = "\n" if original.endswith(("\n", "\r")) else ""
        updated = "\n".join(lines) + ending
        self.write(path, updated)
        return {"path": relative, "line": line, "_before": original, "_after": updated}

    def replace_range(self, path: str, start: int, end: int, value: str) -> dict:
        _, relative, original = self._read_for_edit(path)
        lines = original.splitlines()
        if start < 1 or end < start or end > len(lines):
            raise IndexError("Invalid line range")
        lines[start - 1:end] = value.splitlines()
        ending = "\n" if original.endswith(("\n", "\r")) else ""
        updated = "\n".join(lines) + ending
        self.write(path, updated)
        return {
            "path": relative, "start": start, "end": end,
            "_before": original, "_after": updated,
        }

    def info(self, path: str) -> dict:
        target = self.workspace.resolve(path, must_exist=True)
        stat = target.stat()
        raw = target.read_bytes() if stat.st_size <= MAX_TEXT_FILE_BYTES else None
        raw_bytes = raw or b""
        is_binary = b"\0" in raw_bytes
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        kind = "large" if raw is None else "binary" if is_binary else "text"
        if mime.startswith("image/"): kind = "image"
        elif mime == "application/pdf": kind = "pdf"
        elif "zip" in mime or target.suffix.lower() in {".zip", ".7z", ".rar", ".gz", ".tar"}: kind = "archive"
        return {"path": target.relative_to(self.workspace.root_path).as_posix(), "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns, "hash": hashlib.sha256(raw_bytes).hexdigest() if raw is not None else None,
                "mime": mime, "kind": kind, "extension": target.suffix.lower()}

    def exists(self, path: str) -> bool:
        return self.workspace.resolve(path).exists()

    def hash(self, path: str) -> str:
        return hashlib.sha256(self.workspace.resolve(path, must_exist=True).read_bytes()).hexdigest()

    def delete(self, path: str) -> None:
        target = self.workspace.resolve(path, must_exist=True)
        relative = target.relative_to(self.workspace.root_path).as_posix()
        raw = target.read_bytes()
        expected = self._baselines.get(relative)
        actual = hashlib.sha256(raw).hexdigest()
        if expected and expected != actual: raise ExternalChangeError(relative, expected, actual)
        target.unlink()
        with self._guard: self._baselines.pop(relative, None)
        self._read_cache_drop(relative)

    def forget(self, path: str) -> None:
        relative = self.workspace.resolve(path).relative_to(self.workspace.root_path).as_posix()
        with self._guard: self._baselines.pop(relative, None)
        self._read_cache_drop(relative)

    def remember(self, path: str, *, replace: bool = False) -> str:
        target = self.workspace.resolve(path, must_exist=True)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        relative = target.relative_to(self.workspace.root_path).as_posix()
        with self._guard:
            if replace or relative not in self._baselines: self._baselines[relative] = digest
        return relative
