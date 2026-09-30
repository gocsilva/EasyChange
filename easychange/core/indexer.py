from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path


SKIP_DIRS = {".git", ".easychange", ".venv", "venv", "node_modules", "bin", "obj", "dist", "build",
             "__pycache__", ".gradle", ".idea", ".vs", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
MAX_INDEX_BYTES = 2 * 1024 * 1024
_INDEX_WORKERS = max(2, min(8, os.cpu_count() or 4))

_CS_MODIFIERS = r"(?:(?:public|private|protected|internal|sealed|abstract|static|partial|new|readonly|ref|unsafe)\s+)*"
_SYMBOL_PATTERNS = (
    ("python", re.compile(r"^\s*(?:async\s+def|def)\s+(\w+)\s*\(")),
    ("class", re.compile(rf"^\s*{_CS_MODIFIERS}(?:export\s+)?class\s+(\w+)")),
    ("interface", re.compile(rf"^\s*{_CS_MODIFIERS}(?:export\s+)?interface\s+(\w+)")),
    ("record", re.compile(rf"^\s*{_CS_MODIFIERS}record(?:\s+class|\s+struct)?\s+(\w+)")),
    ("struct", re.compile(rf"^\s*{_CS_MODIFIERS}struct\s+(\w+)")),
    ("enum", re.compile(rf"^\s*{_CS_MODIFIERS}enum\s+(\w+)")),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(")),
    ("method", re.compile(r"^\s*(?:public|private|protected|internal|static|async|virtual|override|sealed|new|\s)*\s*[\w<>?\[\].,]+\s+(\w+)\s*\(([^;]*)\)\s*\{?\s*$")),
)


def _extract_symbols(relative_path: str, content: str) -> list[tuple[str, str, int, int, str]]:
    suffix = Path(relative_path).suffix.casefold()
    output: list[tuple[str, str, int, int, str]] = []
    for line_number, line in enumerate(content.splitlines(), 1):
        patterns = _SYMBOL_PATTERNS[:1] if suffix == ".py" else _SYMBOL_PATTERNS[1:]
        for kind, pattern in patterns:
            match = pattern.match(line)
            if not match:
                continue
            name = match.group(1)
            column = max(1, len(line) - len(line.lstrip()) + 1)
            output.append((name, kind if kind != "python" else "function", line_number, column, line.strip()))
            break
    return output


class Indexer:
    """Persistent hot code index with an instant Git cold path and FTS5 warm path."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.db_path = root / ".easychange" / "index.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._fully_indexed = False
        self._build_thread: threading.Thread | None = None
        self._last_refresh_at = 0.0
        self._last_refresh_result = {"indexed": 0, "removed": 0, "files": 0, "cached": False}
        self._fts_enabled = False
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, extension TEXT, size INTEGER, mtime_ns INTEGER, sha256 TEXT, language TEXT, content TEXT)")
            db.execute("""CREATE TABLE IF NOT EXISTS symbols(
                path TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, line INTEGER NOT NULL,
                column_no INTEGER NOT NULL, signature TEXT NOT NULL, file_hash TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path)")
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(path UNINDEXED, content, tokenize='trigram')")
                self._fts_enabled = True
            except sqlite3.OperationalError:
                self._fts_enabled = False

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA temp_store=MEMORY")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @property
    def ready(self) -> bool:
        return self._fully_indexed

    def start_background_refresh(self) -> None:
        if self._fully_indexed or (self._build_thread and self._build_thread.is_alive()):
            return
        def worker() -> None:
            try:
                self.refresh(force=True)
            except (OSError, sqlite3.Error):
                pass
        self._build_thread = threading.Thread(target=worker, name="easychange-index", daemon=True)
        self._build_thread.start()

    def invalidate(self) -> None:
        self._fully_indexed = False
        self._last_refresh_at = 0.0

    @staticmethod
    def _read_candidate(item: tuple[Path, str, int, int]) -> tuple[str, int, int, str, str | None, list[tuple[str, str, int, int, str]]]:
        path, relative, size, mtime_ns = item
        try:
            raw = path.read_bytes()
        except OSError:
            return relative, size, mtime_ns, "", None, []
        digest = hashlib.sha256(raw).hexdigest()
        content = None
        symbols: list[tuple[str, str, int, int, str]] = []
        if len(raw) <= MAX_INDEX_BYTES and b"\0" not in raw:
            try:
                content = raw.decode("utf-8-sig")
                symbols = _extract_symbols(relative, content)
            except UnicodeDecodeError:
                content = None
        return relative, size, mtime_ns, digest, content, symbols

    def _replace_index_row(self, db, relative: str, extension: str, size: int, mtime_ns: int,
                           digest: str, content: str | None,
                           symbols: list[tuple[str, str, int, int, str]]) -> None:
        db.execute(
            "INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)",
            (relative, extension, size, mtime_ns, digest, _language(extension), content),
        )
        db.execute("DELETE FROM symbols WHERE path=?", (relative,))
        if symbols:
            db.executemany(
                "INSERT INTO symbols(path,name,kind,line,column_no,signature,file_hash) VALUES(?,?,?,?,?,?,?)",
                [(relative, name, kind, line, column, signature, digest)
                 for name, kind, line, column, signature in symbols],
            )
        if self._fts_enabled:
            db.execute("DELETE FROM files_fts WHERE path=?", (relative,))
            if content is not None:
                db.execute("INSERT INTO files_fts(path,content) VALUES(?,?)", (relative, content))

    def update_path(self, relative_path: str) -> dict:
        """Update exactly one known path after an EasyChange mutation."""
        relative = relative_path.replace("\\", "/")
        target = self.root / Path(relative)
        with self._lock, self._connect() as db:
            if not target.exists() or not target.is_file():
                db.execute("DELETE FROM files WHERE path=?", (relative,))
                db.execute("DELETE FROM symbols WHERE path=?", (relative,))
                if self._fts_enabled:
                    db.execute("DELETE FROM files_fts WHERE path=?", (relative,))
                return {"path": relative, "removed": True}
            if any(part in SKIP_DIRS for part in Path(relative).parts):
                return {"path": relative, "ignored": True}
            stat = target.stat()
            relative, size, mtime_ns, digest, content, symbols = self._read_candidate(
                (target, relative, stat.st_size, stat.st_mtime_ns)
            )
            self._replace_index_row(db, relative, target.suffix.lower(), size, mtime_ns, digest, content, symbols)
        self._last_refresh_at = time.monotonic()
        return {"path": relative, "updated": True, "hash": digest[:12]}

    def refresh(self, force: bool = False) -> dict:
        if not force and self._fully_indexed:
            return {**self._last_refresh_result, "cached": True}

        seen: set[str] = set()
        changed: list[tuple[Path, str, int, int]] = []
        with self._lock, self._connect() as db:
            existing = {row[0]: row[1:3] for row in db.execute("SELECT path,size,mtime_ns FROM files")}

        for path in self.root.rglob("*"):
            try:
                if not path.is_file():
                    continue
                relative_path = path.relative_to(self.root)
                if any(part in SKIP_DIRS for part in relative_path.parts):
                    continue
                relative = relative_path.as_posix()
                stat = path.stat()
            except OSError:
                continue
            seen.add(relative)
            if existing.get(relative) != (stat.st_size, stat.st_mtime_ns):
                changed.append((path, relative, stat.st_size, stat.st_mtime_ns))

        if changed:
            with ThreadPoolExecutor(max_workers=_INDEX_WORKERS, thread_name_prefix="easychange-read") as pool:
                loaded = list(pool.map(self._read_candidate, changed))
        else:
            loaded = []

        removed = set(existing) - seen
        with self._lock, self._connect() as db:
            for (path, relative, _, _), row in zip(changed, loaded):
                rel, size, mtime_ns, digest, content, symbols = row
                if not digest:
                    continue
                self._replace_index_row(db, rel, path.suffix.lower(), size, mtime_ns, digest, content, symbols)
            if removed:
                db.executemany("DELETE FROM files WHERE path=?", [(item,) for item in removed])
                db.executemany("DELETE FROM symbols WHERE path=?", [(item,) for item in removed])
                if self._fts_enabled:
                    db.executemany("DELETE FROM files_fts WHERE path=?", [(item,) for item in removed])

        result = {"indexed": len(loaded), "removed": len(removed), "files": len(seen),
                  "cached": False, "fts": self._fts_enabled}
        self._last_refresh_at = time.monotonic()
        self._fully_indexed = True
        self._last_refresh_result = result
        return result

    def files(self, offset: int = 0, limit: int = 100) -> list[dict]:
        if not self._fully_indexed:
            self.refresh()
        with self._connect() as db:
            rows = db.execute(
                "SELECT path,extension,size,mtime_ns,sha256,language FROM files ORDER BY path LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(zip(("path", "extension", "size", "mtime_ns", "hash", "language"), row)) for row in rows]

    def count(self) -> int:
        if not self._fully_indexed:
            self.refresh()
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    def symbols(self, name: str | None = None, path: str | None = None) -> list[dict]:
        # Cold symbol queries should never wait for the full repository index.
        # Resolve only candidate files through git-grep (or the explicit path)
        # while the background index continues warming.
        if not self._fully_indexed and (name is not None or path is not None):
            self.start_background_refresh()
            candidate_paths: list[str] = []
            if path is not None:
                candidate_paths = [path.replace("\\", "/")]
            elif name is not None:
                candidate_paths = list(dict.fromkeys(
                    file_path for file_path, _ in self._git_search(
                        name, regex=False, extension=None, path_prefix=None, case_sensitive=True
                    )
                ))
            if candidate_paths:
                output: list[dict] = []
                for file_path in candidate_paths[:64]:
                    target = self.root / Path(file_path)
                    try:
                        content = target.read_text(encoding="utf-8-sig")
                    except (OSError, UnicodeDecodeError):
                        continue
                    for symbol_name, kind, line, column, signature in _extract_symbols(file_path, content):
                        if name is not None and symbol_name != name:
                            continue
                        output.append({
                            "id": f"S{len(output) + 1}",
                            "file": file_path,
                            "name": symbol_name,
                            "kind": kind,
                            "line": line,
                            "column": column,
                            "signature": signature,
                        })
                return output

        if not self._fully_indexed:
            self.refresh()
        clauses: list[str] = []
        params: list[object] = []
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        if path is not None:
            clauses.append("path = ?")
            params.append(path.replace("\\", "/"))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                "SELECT path,name,kind,line,column_no,signature FROM symbols" + where + " ORDER BY path,line",
                params,
            ).fetchall()
        return [
            {"id": f"S{index}", "file": row[0], "name": row[1], "kind": row[2],
             "line": row[3], "column": row[4], "signature": row[5]}
            for index, row in enumerate(rows, 1)
        ]


    @staticmethod
    def _score_match(query: str, file_path: str, line: str) -> int:
        query_cf = query.casefold()
        path_cf = file_path.casefold()
        name_cf = Path(file_path).name.casefold()
        stem_cf = Path(file_path).stem.casefold()
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
        if re.search(rf"\b(class|interface|enum|record|struct)\s+{re.escape(query)}\b", line, re.I):
            score += 220
        elif re.search(rf"\b{re.escape(query)}\b", line, re.I):
            score += 35
        if line.strip().startswith(("//", "#")):
            score -= 10
        score -= min(30, path_cf.count("/"))
        return score

    def _git_search(self, query: str, *, regex: bool, extension: str | None,
                    path_prefix: str | None, case_sensitive: bool) -> list[tuple[str, str]]:
        if not (self.root / ".git").exists():
            return []
        argv = ["git", "grep", "-n", "-I"]
        if not case_sensitive:
            argv.append("-i")
        argv.append("-E" if regex else "-F")
        argv += [query, "--"]
        if path_prefix:
            argv.append(path_prefix.replace("\\", "/").rstrip("/") + "/**")
        if extension:
            ext = extension if extension.startswith(".") else "." + extension
            argv.append(f"*{ext}")
        try:
            completed = subprocess.run(argv, cwd=self.root, text=True, capture_output=True,
                                       timeout=8, check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if completed.returncode not in {0, 1}:
            return []
        rows: list[tuple[str, str]] = []
        for line in completed.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3:
                rows.append((parts[0].replace("\\", "/"), parts[1] + ":" + parts[2]))
        return rows

    def _candidate_rows(self, query: str, *, regex: bool, extension: str | None,
                        path_prefix: str | None) -> list[tuple[str, str, int, int]]:
        if self._fts_enabled and not regex and len(query) >= 3:
            clauses = ["files_fts MATCH ?"]
            params: list[object] = ['"' + query.replace('"', '""') + '"']
            if extension:
                clauses.append("f.extension = ?")
                params.append(extension if extension.startswith(".") else "." + extension)
            if path_prefix:
                clauses.append("lower(f.path) LIKE ?")
                params.append(path_prefix.replace("\\", "/").casefold().rstrip("/") + "%")
            sql = """SELECT f.path,f.content,f.size,f.mtime_ns
                     FROM files f JOIN files_fts ON files_fts.path=f.path
                     WHERE """ + " AND ".join(clauses)
            try:
                with self._connect() as db:
                    return db.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                pass

        clauses = ["content IS NOT NULL"]
        params = []
        if extension:
            clauses.append("extension = ?")
            params.append(extension if extension.startswith(".") else "." + extension)
        if path_prefix:
            clauses.append("lower(path) LIKE ?")
            params.append(path_prefix.replace("\\", "/").casefold().rstrip("/") + "%")
        if not regex:
            clauses.append("instr(lower(content), ?) > 0")
            params.append(query.casefold())
        with self._connect() as db:
            return db.execute(
                "SELECT path,content,size,mtime_ns FROM files WHERE " + " AND ".join(clauses), params
            ).fetchall()

    def search(self, query: str, *, regex: bool = False, extension: str | None = None,
               path_prefix: str | None = None, case_sensitive: bool = False,
               offset: int = 0, limit: int = 100) -> list[dict]:
        if not self._fully_indexed:
            cold = self._git_search(query, regex=regex, extension=extension,
                                    path_prefix=path_prefix, case_sensitive=case_sensitive)
            self.start_background_refresh()
            if cold:
                found = []
                pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE) if regex else None
                needle = query if case_sensitive else query.casefold()
                for file_path, line_payload in cold:
                    number_text, text = line_payload.split(":", 1)
                    haystack = text if case_sensitive else text.casefold()
                    if (pattern.search(text) if pattern else needle in haystack):
                        found.append({"file": file_path, "line": int(number_text), "text": text.rstrip(),
                                      "score": self._score_match(query, file_path, text)})
                found.sort(key=lambda item: (-int(item["score"]), item["file"].casefold(), int(item["line"])))
                page = found[offset:offset + limit]
                for index, item in enumerate(page, offset + 1):
                    item["id"] = f"R{index}"
                return page
            self.refresh()

        rows = self._candidate_rows(query, regex=regex, extension=extension, path_prefix=path_prefix)

        # Preserve external-edit correctness without making every warm search
        # walk the repository. Validate only candidate files. On a miss in a
        # Git workspace, git-grep can cheaply detect changed tracked content;
        # non-Git workspaces pay for one explicit refresh on a miss.
        stale_paths: list[str] = []
        for file_path, _, indexed_size, indexed_mtime in rows:
            try:
                stat = (self.root / Path(file_path)).stat()
            except OSError:
                stale_paths.append(file_path)
                continue
            if stat.st_size != indexed_size or stat.st_mtime_ns != indexed_mtime:
                stale_paths.append(file_path)
        for file_path in stale_paths:
            self.update_path(file_path)
        if stale_paths:
            rows = self._candidate_rows(query, regex=regex, extension=extension, path_prefix=path_prefix)
        elif not rows:
            if (self.root / ".git").exists():
                live = self._git_search(query, regex=regex, extension=extension,
                                        path_prefix=path_prefix, case_sensitive=case_sensitive)
                touched = {file_path for file_path, _ in live}
                for file_path in touched:
                    self.update_path(file_path)
                if touched:
                    rows = self._candidate_rows(query, regex=regex, extension=extension, path_prefix=path_prefix)
            else:
                self.refresh(force=True)
                rows = self._candidate_rows(query, regex=regex, extension=extension, path_prefix=path_prefix)

        found = []
        pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE) if regex else None
        needle = query if case_sensitive else query.casefold()
        for file_path, content, _, _ in rows:
            if content is None:
                continue
            for number, line in enumerate(content.splitlines(), 1):
                haystack = line if case_sensitive else line.casefold()
                matched = bool(pattern.search(line)) if pattern else needle in haystack
                if matched:
                    found.append({"file": file_path, "line": number, "text": line.rstrip(),
                                  "score": self._score_match(query, file_path, line)})
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
