from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from .workspace import Workspace


class FileService:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._opened: dict[str, Path] = {}

    def list_files(self, limit: int = 500) -> list[dict[str, str]]:
        items = []
        ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".easychange"}
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
        lines = data.decode("utf-8-sig").splitlines()
        start = max(1, start)
        chunk = lines[start - 1:start - 1 + max(1, count)]
        return {"path": target.relative_to(self.workspace.root_path).as_posix(), "start": start,
                "total_lines": len(lines), "hash": hashlib.sha256(data).hexdigest()[:12],
                "lines": [f"{i}|{line}" for i, line in enumerate(chunk, start)]}

    def write(self, path: str, content: str) -> dict:
        target = self.workspace.resolve(path)
        if target.exists() and not target.is_file():
            raise IsADirectoryError(str(target))
        created = not target.exists()
        if target.exists():
            previous_bytes = target.read_bytes()
            if b"\0" in previous_bytes:
                raise ValueError("Binary files cannot be overwritten as text")
        target.parent.mkdir(parents=True, exist_ok=True)
        before = target.read_text(encoding="utf-8") if target.exists() else ""
        target.write_text(content, encoding="utf-8", newline="")
        self._opened[path] = target
        return {"path": target.relative_to(self.workspace.root_path).as_posix(), "created": created,
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
