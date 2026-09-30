from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


SKIP_DIRS = {".git", ".easychange", ".venv", "venv", "node_modules", "bin", "obj", "dist", "build",
             "__pycache__", ".gradle", ".idea", ".vs", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
MAX_INDEX_BYTES = 2 * 1024 * 1024


class Indexer:
    """Incremental metadata and text index backed by SQLite."""
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db_path = root / ".easychange" / "index.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, extension TEXT, size INTEGER, mtime_ns INTEGER, sha256 TEXT, language TEXT, content TEXT)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def refresh(self) -> dict:
        seen = set()
        updated = 0
        with self._lock, self._connect() as db:
            existing = {row[0]: row[1:3] for row in db.execute("SELECT path,size,mtime_ns FROM files")}
            for path in self.root.rglob("*"):
                if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(self.root).parts):
                    continue
                relative = path.relative_to(self.root).as_posix()
                seen.add(relative)
                try:
                    stat = path.stat()
                    if existing.get(relative) == (stat.st_size, stat.st_mtime_ns):
                        continue
                    raw = path.read_bytes()
                    digest = hashlib.sha256(raw).hexdigest()
                    content = None
                    if len(raw) <= MAX_INDEX_BYTES and b"\0" not in raw:
                        try: content = raw.decode("utf-8-sig")
                        except UnicodeDecodeError: pass
                    extension = path.suffix.lower()
                    language = _language(extension)
                    db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)",
                               (relative, extension, stat.st_size, stat.st_mtime_ns, digest, language, content))
                    updated += 1
                except OSError:
                    continue
            removed = set(existing) - seen
            if removed:
                db.executemany("DELETE FROM files WHERE path=?", [(item,) for item in removed])
        return {"indexed": updated, "removed": len(removed) if 'removed' in locals() else 0, "files": len(seen)}

    def files(self, offset: int = 0, limit: int = 100) -> list[dict]:
        self.refresh()
        with self._connect() as db:
            rows = db.execute("SELECT path,extension,size,mtime_ns,sha256,language FROM files ORDER BY path LIMIT ? OFFSET ?",
                              (limit, offset)).fetchall()
        return [dict(zip(("path", "extension", "size", "mtime_ns", "hash", "language"), row)) for row in rows]

    def count(self) -> int:
        self.refresh()
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    def search(self, query: str, *, regex: bool = False, extension: str | None = None,
               path_prefix: str | None = None, case_sensitive: bool = False, offset: int = 0, limit: int = 100) -> list[dict]:
        self.refresh()
        with self._connect() as db:
            rows = db.execute("SELECT path,content FROM files WHERE content IS NOT NULL ORDER BY path").fetchall()
        found = []
        import re
        pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE) if regex else None
        for file_path, content in rows:
            if extension and Path(file_path).suffix.casefold() != extension.casefold(): continue
            if path_prefix and not file_path.casefold().startswith(path_prefix.casefold().replace("\\", "/")): continue
            for number, line in enumerate(content.splitlines(), 1):
                haystack = line if case_sensitive else line.casefold()
                matched = bool(pattern.search(line)) if pattern else (query if case_sensitive else query.casefold()) in haystack
                if matched:
                    found.append({"id": f"R{len(found)+1}", "file": file_path, "line": number, "text": line.rstrip()})
        return found[offset:offset + limit]


def _language(extension: str) -> str:
    return {".py": "python", ".cs": "csharp", ".js": "javascript", ".ts": "typescript",
            ".java": "java", ".kt": "kotlin", ".rs": "rust", ".go": "go", ".cpp": "cpp",
            ".c": "c", ".h": "c", ".php": "php", ".rb": "ruby", ".ps1": "powershell",
            ".sh": "shell", ".html": "html", ".css": "css", ".json": "json", ".md": "markdown"}.get(extension, "text")
