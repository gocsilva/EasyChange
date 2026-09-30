from __future__ import annotations

import difflib
import hashlib
import mimetypes
import threading
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

    @property
    def baselines(self) -> dict[str, str]:
        with self._guard: return dict(self._baselines)

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
        data = target.read_bytes()
        if b"\0" in data:
            raise ValueError("Binary files cannot be read as text")
        if len(data) > MAX_TEXT_FILE_BYTES:
            raise ValueError(f"File exceeds text read limit ({MAX_TEXT_FILE_BYTES} bytes)")
        lines = data.decode("utf-8-sig").splitlines()
        relative = target.relative_to(self.workspace.root_path).as_posix()
        with self._guard:
            self._baselines[relative] = hashlib.sha256(data).hexdigest()
        start = max(1, start)
        chunk = lines[start - 1:start - 1 + max(1, count)]
        return {"path": relative, "start": start,
                "total_lines": len(lines), "hash": hashlib.sha256(data).hexdigest()[:12],
                "lines": [f"{i}|{line}" for i, line in enumerate(chunk, start)]}

    def write(self, path: str, content: str, *, force: bool = False) -> dict:
        target = self.workspace.resolve(path)
        relative = target.relative_to(self.workspace.root_path).as_posix()
        if target.exists() and not target.is_file():
            raise IsADirectoryError(str(target))
        created = not target.exists()
        if target.exists():
            previous_bytes = target.read_bytes()
            if b"\0" in previous_bytes:
                raise ValueError("Binary files cannot be overwritten as text")
            if len(previous_bytes) > MAX_TEXT_FILE_BYTES:
                raise ValueError(f"File exceeds text write limit ({MAX_TEXT_FILE_BYTES} bytes)")
            actual_hash = hashlib.sha256(previous_bytes).hexdigest()
            with self._guard:
                expected_hash = self._baselines.get(relative)
            if expected_hash and actual_hash != expected_hash and not force:
                raise ExternalChangeError(relative, expected_hash, actual_hash)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = target.read_text(encoding="utf-8") if target.exists() else ""
        target.write_text(content, encoding="utf-8", newline="")
        self._opened[path] = target
        with self._guard:
            self._baselines[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
        return {"path": relative, "created": created,
                "diff": "".join(difflib.unified_diff(before.splitlines(True), content.splitlines(True),
                        fromfile="before", tofile="after"))}

    def replace(self, path: str, old: str, new: str, count: int = 0) -> dict:
        target = self.workspace.resolve(path, must_exist=True)
        original = target.read_text(encoding="utf-8")
        occurrences = original.count(old)
        if not occurrences:
            raise LookupError("Search text was not found")
        updated = original.replace(old, new, count) if count else original.replace(old, new)
        self.write(path, updated)
        return {"path": path, "replacements": min(occurrences, count) if count else occurrences}

    def replace_line(self, path: str, line: int, value: str) -> dict:
        target = self.workspace.resolve(path, must_exist=True)
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        if line < 1 or line > len(lines):
            raise IndexError(f"Line out of range: {line}")
        ending = "\n" if lines[line - 1].endswith("\n") else ""
        lines[line - 1] = value + ending
        self.write(path, "".join(lines))
        return {"path": path, "line": line}

    def append(self, path: str, value: str) -> dict:
        target = self.workspace.resolve(path)
        before = target.read_text(encoding="utf-8") if target.exists() else ""
        separator = "" if not before or before.endswith(("\n", "\r")) else "\n"
        self.write(path, before + separator + value)
        return {"path": path, "appended_chars": len(value)}

    def insert_line(self, path: str, line: int, value: str) -> dict:
        target = self.workspace.resolve(path, must_exist=True)
        original = target.read_text(encoding="utf-8")
        lines = original.splitlines()
        if line < 1 or line > len(lines) + 1:
            raise IndexError(f"Line out of range: {line}")
        lines.insert(line - 1, value)
        ending = "\n" if original.endswith(("\n", "\r")) else ""
        self.write(path, "\n".join(lines) + ending)
        return {"path": path, "line": line}

    def replace_range(self, path: str, start: int, end: int, value: str) -> dict:
        target = self.workspace.resolve(path, must_exist=True)
        original = target.read_text(encoding="utf-8")
        lines = original.splitlines()
        if start < 1 or end < start or end > len(lines):
            raise IndexError("Invalid line range")
        lines[start - 1:end] = value.splitlines()
        ending = "\n" if original.endswith(("\n", "\r")) else ""
        self.write(path, "\n".join(lines) + ending)
        return {"path": path, "start": start, "end": end}

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

    def forget(self, path: str) -> None:
        relative = self.workspace.resolve(path).relative_to(self.workspace.root_path).as_posix()
        with self._guard: self._baselines.pop(relative, None)

    def remember(self, path: str, *, replace: bool = False) -> str:
        target = self.workspace.resolve(path, must_exist=True)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        relative = target.relative_to(self.workspace.root_path).as_posix()
        with self._guard:
            if replace or relative not in self._baselines: self._baselines[relative] = digest
        return relative
