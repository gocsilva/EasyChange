from __future__ import annotations
import time

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


SKIP_DIRS = {".git", ".easychange", ".venv", "venv", "node_modules", "bin", "obj", "dist", "build",
             "__pycache__", ".gradle", ".idea", ".vs", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
MAX_INDEX_BYTES = 2 * 1024 * 1024


class Indexer:
    """Incremental metadata/text index optimized for repeated AI queries."""

    REFRESH_MIN_INTERVAL = 0.75

    def __init__(self, root: Path) -> None:
        self.root = root
        self.db_path = root / ".easychange" / "index.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._last_refresh_at = 0.0
        self._fully_indexed = False
        self._last_refresh_result = {"indexed": 0, "removed": 0, "files": 0, "cached": False}
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, extension TEXT, size INTEGER, mtime_ns INTEGER, sha256 TEXT, language TEXT, content TEXT)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def invalidate(self) -> None:
        self._fully_indexed = False
        self._last_refresh_at = 0.0

    def update_path(self, relative_path: str) -> dict:
        """Update only one known path after an EasyChange mutation."""
        relative = relative_path.replace("\\", "/")
        target = self.root / Path(relative)
        with self._lock, self._connect() as db:
            if not target.exists() or not target.is_file():
                db.execute("DELETE FROM files WHERE path=?", (relative,))
                self.invalidate()
                return {"path": relative, "removed": True}
            if any(part in SKIP_DIRS for part in Path(relative).parts):
                return {"path": relative, "ignored": True}
            stat = target.stat()
            raw = target.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            content = None
            if len(raw) <= MAX_INDEX_BYTES and b"\0" not in raw:
                try:
                    content = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    pass
            extension = target.suffix.lower()
            db.execute(
                "INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)",
                (relative, extension, stat.st_size, stat.st_mtime_ns, digest, _language(extension), content),
            )
        self._last_refresh_at = time.monotonic()
        return {"path": relative, "updated": True, "hash": digest[:12]}

    def refresh(self, force: bool = False) -> dict:
        if not force and self._fully_indexed:
            return {**self._last_refresh_result, "cached": True}
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
                        try:
                            content = raw.decode("utf-8-sig")
                        except UnicodeDecodeError:
                            pass
                    extension = path.suffix.lower()
                    db.execute(
                        "INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)",
                        (relative, extension, stat.st_size, stat.st_mtime_ns, digest, _language(extension), content),
                    )
                    updated += 1
                except OSError:
                    continue
            removed = set(existing) - seen
            if removed:
                db.executemany("DELETE FROM files WHERE path=?", [(item,) for item in removed])
        result = {"indexed": updated, "removed": len(removed), "files": len(seen), "cached": False}
        self._last_refresh_at = time.monotonic()
        self._fully_indexed = True
        self._last_refresh_result = result
        return result

    def files(self, offset: int = 0, limit: int = 100) -> list[dict]:
        self.refresh()
        with self._connect() as db:
            rows = db.execute(
                "SELECT path,extension,size,mtime_ns,sha256,language FROM files ORDER BY path LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(zip(("path", "extension", "size", "mtime_ns", "hash", "language"), row)) for row in rows]

    def count(self) -> int:
        self.refresh()
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    @staticmethod
    def _score_match(query: str, file_path: str, line: str) -> int:
        query_cf = query.casefold()
        path_cf = file_path.casefold()
        name_cf = Path(file_path).name.casefold()
        stem_cf = Path(file_path).stem.casefold()
        line_cf = line.casefold()
        score = 0
        if stem_cf == query_cf:
            score += 260
        elif query_cf in name_cf:
            score += 130
        if path_cf.startswith("src/") or "/src/" in path_cf:
            score += 80
        if path_cf.startswith("tests/") or "/tests/" in path_cf or "unittests" in path_cf:
            score += 55
        if path_cf.startswith("docs/") or "/docs/" in path_cf:
            score -= 80
        if path_cf.endswith(".md"):
            score -= 50
        definition = re.search(rf"\b(class|interface|enum|record|struct)\s+{re.escape(query)}\b", line, re.I)
        if definition:
            score += 220
        elif re.search(rf"\b{re.escape(query)}\b", line, re.I):
            score += 35
        if line_cf.strip().startswith("//") or line_cf.strip().startswith("#"):
            score -= 10
        score -= min(30, path_cf.count("/"))
        return score

    def search(self, query: str, *, regex: bool = False, extension: str | None = None,
               path_prefix: str | None = None, case_sensitive: bool = False, offset: int = 0, limit: int = 100) -> list[dict]:
        refresh_state = self.refresh()
        clauses = ["content IS NOT NULL"]
        params: list[object] = []
        if extension:
            clauses.append("extension = ?")
            params.append(extension if extension.startswith(".") else "." + extension)
        if path_prefix:
            clauses.append("lower(path) LIKE ?")
            params.append(path_prefix.replace("\\", "/").casefold().rstrip("/") + "%")
        if not regex:
            if case_sensitive:
                clauses.append("instr(content, ?) > 0")
                params.append(query)
            else:
                clauses.append("instr(lower(content), ?) > 0")
                params.append(query.casefold())

        sql = (
            "SELECT path,content,size,mtime_ns FROM files WHERE "
            + " AND ".join(clauses)
        )

        def load_rows():
            with self._connect() as db:
                return db.execute(sql, params).fetchall()

        rows = load_rows()

        # Fast cached queries remain O(candidate-files). If the cached index
        # can prove itself stale (or has no candidates for a new term), pay for
        # one forced refresh to preserve correctness for external edits.
        if refresh_state.get("cached"):
            stale = not rows
            if not stale:
                for file_path, _, indexed_size, indexed_mtime in rows:
                    try:
                        stat = (self.root / Path(file_path)).stat()
                    except OSError:
                        stale = True
                        break
                    if stat.st_size != indexed_size or stat.st_mtime_ns != indexed_mtime:
                        stale = True
                        break
            if stale:
                self.refresh(force=True)
                rows = load_rows()

        found = []
        pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE) if regex else None
        needle = query if case_sensitive else query.casefold()
        for file_path, content, _, _ in rows:
            for number, line in enumerate(content.splitlines(), 1):
                haystack = line if case_sensitive else line.casefold()
                matched = bool(pattern.search(line)) if pattern else needle in haystack
                if matched:
                    found.append({
                        "file": file_path,
                        "line": number,
                        "text": line.rstrip(),
                        "score": self._score_match(query, file_path, line),
                    })
        found.sort(key=lambda item: (-int(item["score"]), item["file"].casefold(), int(item["line"])))
        page = found[offset:offset + limit]
        for index, item in enumerate(page, offset + 1):
            item["id"] = f"R{index}"
        return page


def _language(extension: str) -> str:
    return {".py": "python", ".cs": "csharp", ".js": "javascript", ".ts": "typescript",
            ".java": "java", ".kt": "kotlin", ".rs": "rust", ".go": "go", ".cpp": "cpp",
            ".c": "c", ".h": "c", ".php": "php", ".rb": "ruby", ".ps1": "powershell",
            ".sh": "shell", ".html": "html", ".css": "css", ".json": "json", ".md": "markdown"}.get(extension, "text")
