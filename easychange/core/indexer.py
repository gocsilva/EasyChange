from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .subprocess_utils import hidden_console_kwargs


class _CasefoldSkipDirs(set):
    """Skip workspace internals regardless of directory case on Windows.

    All indexer paths (walk, incremental updates, cold/hot search and listing)
    share this membership gate, so case variants cannot bypass exclusions.
    """
    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and super().__contains__(value.casefold())


SKIP_DIRS = _CasefoldSkipDirs({
    ".git", ".easychange", ".venv", "venv", "node_modules", "bin", "obj",
    "dist", "build", "__pycache__", ".gradle", ".idea", ".vs",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
})
MAX_INDEX_BYTES = 2 * 1024 * 1024
# Git may print matches from every tracked/untracked source. Bound what
# cold discovery materializes even when the process writes many gigabytes.
MAX_COLD_GIT_BYTES = 8 * 1024 * 1024
MAX_COLD_GIT_ROWS = 20_000
MAX_INDEX_PAGE_ROWS = 100_000


def _validate_index_page(offset: int, limit: int) -> None:
    # SQLite interprets LIMIT -1 as unlimited. Public/bridge index calls must
    # never turn malformed pagination into a full inventory materialization.
    if (type(offset) is not int or offset < 0 or offset > 2**63 - 1
            or type(limit) is not int or not 0 <= limit <= MAX_INDEX_PAGE_ROWS):
        raise ValueError("INVALID_INDEX_PAGE")
# Cold Git-backed discovery already works before the persistent index is warm.
# Keep background hashing deliberately modest so startup/index refresh does not
# steal CPU/disk from Qt painting and EC2 QR delivery.
_INDEX_WORKERS = max(2, min(4, max(2, (os.cpu_count() or 4) // 2)))
_INDEX_VERSION = "2"

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
    output: list[tuple[str, str, int, int, str]] = []
    for line_number, line in enumerate(content.splitlines(), 1):
        for kind, pattern in _SYMBOL_PATTERNS:
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
        self._protected_paths: set[str] = set()
        # Windows workspaces are typically case-insensitive. Match protected
        # paths with Unicode casefold even when Git/SQLite stored another case.
        self._protected_keys: set[str] = set()
        self._fully_indexed = False
        self._build_thread: threading.Thread | None = None
        self._last_refresh_at = 0.0
        self._last_refresh_result = {
            "indexed": 0, "removed": 0, "files": 0, "cached": False,
            "workers": _INDEX_WORKERS,
        }
        self._fts_enabled = False
        self._force_rebuild = False
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, extension TEXT, size INTEGER, mtime_ns INTEGER, sha256 TEXT, language TEXT, content TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS index_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
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
            current_version = db.execute("SELECT value FROM index_meta WHERE key='version'").fetchone()
            self._force_rebuild = not current_version or current_version[0] != _INDEX_VERSION
            protected = db.execute(
                "SELECT value FROM index_meta WHERE key='protected_paths_v1'"
            ).fetchone()
            self._protected_paths = self._decode_protected_paths(
                protected[0] if protected is not None else None)
            self._protected_keys = {
                item.casefold() for item in self._protected_paths
            }

    @classmethod
    def _decode_protected_paths(cls, raw) -> set[str]:
        if raw is None:
            return set()
        if not isinstance(raw, str) or len(raw) > 1_048_576:
            raise RuntimeError("INDEXER_PROTECTED_PATHS_CORRUPT")
        try:
            decoded = json.loads(raw)
            if (not isinstance(decoded, list) or len(decoded) > 10_000
                    or any(type(item) is not str or len(item) > 4096
                           for item in decoded)):
                raise ValueError("bad protected path metadata")
            return {cls._normalize_relative_path(item) for item in decoded}
        except (TypeError, ValueError, RecursionError) as exc:
            raise RuntimeError("INDEXER_PROTECTED_PATHS_CORRUPT") from exc

    def _sync_protected_paths(self) -> None:
        # A second EasyChange process may protect a file after this indexer
        # opened its SQLite connection. Refresh the small exclusion metadata
        # before source search/index operations, not for every scanned file.
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT value FROM index_meta WHERE key='protected_paths_v1'"
            ).fetchone()
            stored = self._decode_protected_paths(row[0] if row else None)
            self._protected_paths.update(stored)
            self._protected_keys.update(item.casefold() for item in stored)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA temp_store=MEMORY")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def status(self) -> dict:
        return {
            "state": "READY" if self._fully_indexed else "WARMING",
            "fts5": self._fts_enabled,
            "background": bool(self._build_thread and self._build_thread.is_alive()),
            "last_refresh": dict(self._last_refresh_result),
        }

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

    def close(self, timeout: float = 15.0) -> None:
        """Wait for background index warm-up so SQLite handles are closed before workspace teardown."""
        thread = self._build_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(max(0.1, float(timeout)))

    def invalidate(self) -> None:
        self._fully_indexed = False
        self._last_refresh_at = 0.0

    @staticmethod
    def _read_candidate(
        item: tuple[Path, str, int, int], *,
        trusted_root: Path | None = None,
    ) -> tuple[str, int, int, str, str | None, list[tuple[str, str, int, int, str]]]:
        path, relative, size, mtime_ns = item
        skipped = (relative, size, mtime_ns, "", None, [])
        try:
            if trusted_root is not None:
                # Callers pass the canonical root computed once per scan.
                # Resolving it again for each file turns large refreshes into
                # repeated filesystem metadata work without strengthening the
                # per-candidate symlink and containment checks below.
                root = trusted_root
                # The directory walker checked this earlier, but a file or
                # ancestor may have become a symlink while waiting for a read
                # worker. Check immediately before opening the candidate.
                current = root
                for part in Path(relative).parts:
                    current = current / part
                    if current.is_symlink():
                        return skipped
                if not path.resolve().is_relative_to(root):
                    return skipped
            with path.open("rb") as stream:
                if trusted_root is not None:
                    if (path.is_symlink() or
                            not path.resolve().is_relative_to(root)):
                        return skipped
                    metadata = os.fstat(stream.fileno())
                    if (metadata.st_size != size or
                            metadata.st_mtime_ns != mtime_ns):
                        return skipped
                # Hash large assets in bounded chunks; only small source
                # files need to reside in memory for symbol extraction.
                prefix = stream.read(MAX_INDEX_BYTES + 1)
                hasher = hashlib.sha256(prefix)
                oversized = len(prefix) > MAX_INDEX_BYTES
                if oversized:
                    for block in iter(lambda: stream.read(128 * 1024), b""):
                        hasher.update(block)
        except (OSError, RuntimeError):
            return skipped
        digest = hasher.hexdigest()
        content = None
        symbols: list[tuple[str, str, int, int, str]] = []
        if not oversized and b"\0" not in prefix:
            try:
                content = prefix.decode("utf-8-sig")
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


    @staticmethod
    def _normalize_relative_path(relative_path: str) -> str:
        value = str(relative_path or "").replace("\\", "/").strip()
        if value.startswith("/") or re.match(r"^[a-zA-Z]:", value):
            raise ValueError("INVALID_PROTECTED_INDEX_PATH")
        while value.startswith("./"):
            value = value[2:]
        value = value.rstrip("/")
        if (
            not value
            or any(part in ("", ".", "..") for part in value.split("/"))
        ):
            raise ValueError("INVALID_PROTECTED_INDEX_PATH")
        return value

    def _has_symlink_component(self, relative_path: str) -> bool:
        # A symlink in a parent can redirect a normal-looking file path into
        # an excluded directory. Check each component of targeted reads.
        current=self.root
        for component in Path(relative_path).parts:
            current=current/component
            if current.is_symlink():
                return True
        return False

    def _is_protected_path(self, relative_path: str) -> bool:
        return relative_path.casefold() in self._protected_keys

    def protect_path(self, relative_path: str, *, scrub: bool = True) -> dict:
        """Exclude a known sensitive workspace file and purge prior indexed content."""
        relative = self._normalize_relative_path(relative_path)
        with self._lock:
            self._protected_paths.add(relative)
            self._protected_keys.add(relative.casefold())
            with self._connect() as db:
                # Serialize protection registrations from different processes
                # rather than letting a stale instance overwrite another
                # process's protected path list.
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT value FROM index_meta WHERE key='protected_paths_v1'"
                ).fetchone()
                self._protected_paths.update(
                    self._decode_protected_paths(row[0] if row else None))
                self._protected_keys.update(
                    item.casefold() for item in self._protected_paths)
                encoded = json.dumps(sorted(self._protected_paths))
                if len(encoded) > 1_048_576:
                    raise ValueError("INDEXER_PROTECTED_PATHS_LIMIT")
                db.execute(
                    "INSERT OR REPLACE INTO index_meta(key,value) VALUES(?,?)",
                    ("protected_paths_v1", encoded),
                )
                # An old index can contain a different on-disk capitalization
                # of the same Windows path. Scrub all aliases, including FTS.
                for table in (("files", "symbols", "files_fts")
                              if self._fts_enabled else ("files", "symbols")):
                    paths = [
                        row[0] for row in db.execute(
                            f"SELECT DISTINCT path FROM {table}"
                        ) if isinstance(row[0], str)
                        and row[0].casefold() == relative.casefold()
                    ]
                    db.executemany(
                        f"DELETE FROM {table} WHERE path=?",
                        ((item,) for item in paths),
                    )
            if scrub:
                # Deleted SQLite cells/WAL frames may otherwise retain plaintext.
                # Rebuild locally after the targeted delete; secret-source imports
                # are rare and bounded to small files, so correctness wins here.
                db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
                try:
                    db.execute("PRAGMA secure_delete=ON")
                    try:
                        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.OperationalError:
                        pass
                    db.execute("VACUUM")
                    try:
                        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.OperationalError:
                        pass
                finally:
                    db.close()
        return {"path": relative, "protected": True, "scrubbed": bool(scrub)}

    def protected_paths(self) -> list[str]:
        with self._lock:
            return sorted(self._protected_paths)
    def update_path(self, relative_path: str) -> dict:
        """Update exactly one known path after an EasyChange mutation."""
        relative = self._normalize_relative_path(relative_path)
        self._sync_protected_paths()
        target = self.root / Path(relative)
        try:
            trusted_root = self.root.resolve()
            # A symlink within the workspace is also handled by the regular
            # scan, which deliberately excludes links to avoid duplicate or
            # externally remapped source records.
            if (self._has_symlink_component(relative) or
                    not target.resolve().is_relative_to(trusted_root)):
                raise ValueError("INVALID_PROTECTED_INDEX_PATH")
        except (OSError, RuntimeError) as exc:
            raise ValueError("INVALID_PROTECTED_INDEX_PATH") from exc
        # Hash the candidate before reserving SQLite's single-writer lock.
        # A large binary file can take seconds to read; only the short
        # exclusion recheck and SQL changes belong inside the transaction.
        candidate = None
        if (not self._is_protected_path(relative) and
                not any(part in SKIP_DIRS for part in Path(relative).parts)):
            try:
                if target.is_file():
                    metadata = target.stat()
                    candidate = self._read_candidate(
                        (target, relative, metadata.st_size, metadata.st_mtime_ns),
                        trusted_root=trusted_root,
                    )
            except (OSError, RuntimeError):
                candidate = None
        with self._lock, self._connect() as db:
            # protect_path() may commit in another process after our initial
            # _sync_protected_paths(). Hold SQLite's writer reservation and
            # reread exclusions before indexing any bytes from this path.
            db.execute("BEGIN IMMEDIATE")
            protected_row = db.execute(
                "SELECT value FROM index_meta WHERE key='protected_paths_v1'"
            ).fetchone()
            latest = self._decode_protected_paths(
                protected_row[0] if protected_row else None)
            self._protected_paths.update(latest)
            self._protected_keys.update(item.casefold() for item in latest)
            if self._is_protected_path(relative):
                db.execute("DELETE FROM files WHERE path=?", (relative,))
                db.execute("DELETE FROM symbols WHERE path=?", (relative,))
                if self._fts_enabled:
                    db.execute("DELETE FROM files_fts WHERE path=?", (relative,))
                return {"path": relative, "ignored": True, "protected": True}
            if any(part in SKIP_DIRS for part in Path(relative).parts):
                # This path could exist in a legacy index from before it was
                # excluded. Remove it now, rather than reporting ignored while
                # continuing to return stale indexed content.
                db.execute("DELETE FROM files WHERE path=?", (relative,))
                db.execute("DELETE FROM symbols WHERE path=?", (relative,))
                if self._fts_enabled:
                    db.execute("DELETE FROM files_fts WHERE path=?", (relative,))
                return {"path": relative, "ignored": True}
            if not target.exists() or not target.is_file():
                db.execute("DELETE FROM files WHERE path=?", (relative,))
                db.execute("DELETE FROM symbols WHERE path=?", (relative,))
                if self._fts_enabled:
                    db.execute("DELETE FROM files_fts WHERE path=?", (relative,))
                return {"path": relative, "removed": True}
            if candidate is None:
                digest = ""
            else:
                _, size, mtime_ns, digest, content, symbols = candidate
                try:
                    current = target.stat()
                    # The bytes were read without the database writer lock.
                    # Do not persist a snapshot of a file that was replaced,
                    # redirected through a symlink, or modified meanwhile.
                    if (current.st_size != size or current.st_mtime_ns != mtime_ns
                            or self._has_symlink_component(relative)
                            or not target.resolve().is_relative_to(self.root.resolve())):
                        digest = ""
                except (OSError, RuntimeError):
                    digest = ""
            if not digest:
                # A disappearing/replaced file must not revive stale symbols
                # or be indexed as a valid source with an empty SHA-256.
                db.execute("DELETE FROM files WHERE path=?", (relative,))
                db.execute("DELETE FROM symbols WHERE path=?", (relative,))
                if self._fts_enabled:
                    db.execute("DELETE FROM files_fts WHERE path=?", (relative,))
                return {"path": relative, "removed": True}
            self._replace_index_row(db, relative, target.suffix.lower(), size, mtime_ns, digest, content, symbols)
        self._last_refresh_at = time.monotonic()
        return {"path": relative, "updated": True, "hash": digest[:12]}

    def refresh(self, force: bool = False) -> dict:
        self._sync_protected_paths()
        if not force and self._fully_indexed:
            return {**self._last_refresh_result, "cached": True}

        seen: set[str] = set()
        changed: list[tuple[Path, str, int, int]] = []
        with self._lock, self._connect() as db:
            persisted = {row[0]: row[1:3] for row in db.execute("SELECT path,size,mtime_ns FROM files")}
        existing = {} if self._force_rebuild else persisted
        # Resolve the trusted workspace boundary once per scan, not once
        # for every source file in a large repository.
        trusted_root = self.root.resolve()

        # rglob("*") descends into .git, node_modules and virtualenvs
        # before the per-file SKIP_DIRS check can reject their contents.
        # Prune those subtrees before enumeration to avoid enormous wasted
        # directory I/O on real projects. Never follow directory symlinks.
        for folder, directories, filenames in os.walk(
                self.root, topdown=True, followlinks=False):
            directories[:] = [
                name for name in directories
                if name not in SKIP_DIRS
                and not (Path(folder) / name).is_symlink()
            ]
            for filename in filenames:
                path = Path(folder) / filename
                try:
                    if not path.is_file() or path.is_symlink():
                        continue
                    if not path.resolve().is_relative_to(trusted_root):
                        continue
                    relative_path = path.relative_to(self.root)
                    if any(part in SKIP_DIRS for part in relative_path.parts):
                        continue
                    relative = relative_path.as_posix()
                    if self._is_protected_path(relative):
                        continue
                    stat = path.stat()
                except (OSError, RuntimeError):
                    # Cyclic links may raise RuntimeError during resolution.
                    continue
                seen.add(relative)
                if existing.get(relative) != (stat.st_size, stat.st_mtime_ns):
                    changed.append((path, relative, stat.st_size, stat.st_mtime_ns))

        loaded_count = 0
        if changed:
            # Future.map() for *every* changed file can queue hundreds of
            # decoded 2 MiB sources in RAM, even with just four workers.
            # Read/write bounded batches; release the index lock between
            # disk reads so GUI actions are not blocked by the cold scan.
            batch_size = _INDEX_WORKERS * 4
            with ThreadPoolExecutor(max_workers=_INDEX_WORKERS, thread_name_prefix="easychange-read") as pool:
                for offset in range(0, len(changed), batch_size):
                    batch = changed[offset:offset + batch_size]
                    loaded = list(pool.map(
                        lambda item: self._read_candidate(
                            item, trusted_root=trusted_root), batch))
                    with self._lock, self._connect() as db:
                        # A second process can protect a source after this
                        # batch read it, but before we save its decoded text.
                        # Serialize with protect_path() via BEGIN IMMEDIATE,
                        # then refresh the exclusion list under that lock.
                        db.execute("BEGIN IMMEDIATE")
                        protected_row = db.execute(
                            "SELECT value FROM index_meta WHERE key='protected_paths_v1'"
                        ).fetchone()
                        latest = self._decode_protected_paths(
                            protected_row[0] if protected_row else None)
                        self._protected_paths.update(latest)
                        self._protected_keys.update(
                            item.casefold() for item in latest)
                        for (path, relative, _, _), row in zip(batch, loaded):
                            rel, size, mtime_ns, digest, content, symbols = row
                            if self._is_protected_path(rel):
                                db.execute("DELETE FROM files WHERE path=?", (rel,))
                                db.execute("DELETE FROM symbols WHERE path=?", (rel,))
                                if self._fts_enabled:
                                    db.execute("DELETE FROM files_fts WHERE path=?", (rel,))
                                continue
                            if not digest:
                                # If a previously indexed file vanished or
                                # changed identity after enumeration, retire
                                # its old content rather than leaving stale
                                # code or protected symbols queryable.
                                db.execute("DELETE FROM files WHERE path=?", (rel,))
                                db.execute("DELETE FROM symbols WHERE path=?", (rel,))
                                if self._fts_enabled:
                                    db.execute("DELETE FROM files_fts WHERE path=?", (rel,))
                                continue
                            self._replace_index_row(db, rel, path.suffix.lower(), size, mtime_ns, digest, content, symbols)
                            loaded_count += 1
                    # Do not retain the completed batch while the next one
                    # hashes large, potentially binary files.
                    del loaded

        removed = set(persisted) - seen
        with self._lock, self._connect() as db:
            if removed:
                db.executemany("DELETE FROM files WHERE path=?", [(item,) for item in removed])
                db.executemany("DELETE FROM symbols WHERE path=?", [(item,) for item in removed])
                if self._fts_enabled:
                    db.executemany("DELETE FROM files_fts WHERE path=?", [(item,) for item in removed])
            db.execute("INSERT OR REPLACE INTO index_meta(key,value) VALUES('version',?)", (_INDEX_VERSION,))

        self._force_rebuild = False
        result = {
            "indexed": loaded_count, "removed": len(removed), "files": len(seen),
            "cached": False, "fts": self._fts_enabled, "workers": _INDEX_WORKERS,
        }
        self._last_refresh_at = time.monotonic()
        self._fully_indexed = True
        self._last_refresh_result = result
        return result

    def files(self, offset: int = 0, limit: int = 100) -> list[dict]:
        _validate_index_page(offset, limit)
        if limit == 0:
            return []
        self._sync_protected_paths()
        if not self._fully_indexed:
            self.refresh()
        with self._connect() as db:
            if not self._protected_keys:
                rows = db.execute(
                    "SELECT path,extension,size,mtime_ns,sha256,language FROM files ORDER BY path LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            else:
                # A stale index row must not leak a protected filename. Page
                # only after filtering; applying SQL OFFSET first would hide
                # valid entries when a protected row sorts before them.
                rows = []
                skipped = 0
                cursor = db.execute(
                    "SELECT path,extension,size,mtime_ns,sha256,language FROM files ORDER BY path"
                )
                for row in cursor:
                    if (self._is_protected_path(row[0]) or
                            any(part in SKIP_DIRS for part in Path(row[0]).parts)):
                        continue
                    if skipped < offset:
                        skipped += 1
                        continue
                    rows.append(row)
                    if len(rows) >= limit:
                        break
        return [dict(zip(("path", "extension", "size", "mtime_ns", "hash", "language"), row)) for row in rows]

    def count(self) -> int:
        if not self._fully_indexed:
            self.refresh()
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    def symbols(self, name: str | None = None, path: str | None = None) -> list[dict]:
        self._sync_protected_paths()
        # A cold symbol lookup must obey the same path protections as the
        # indexed/warm path. Otherwise a supplied ../ path or a protected
        # secret could be read directly before the index has warmed.
        if path is not None:
            path = self._normalize_relative_path(path)
            if (self._is_protected_path(path) or
                    any(part in SKIP_DIRS for part in Path(path).parts)):
                return []
        def load_rows() -> list[tuple]:
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
                return db.execute(
                    "SELECT path,name,kind,line,column_no,signature FROM symbols"
                    + where + " ORDER BY path,line",
                    params,
                ).fetchall()

        # Cold definition queries should not block on a full Git workspace scan.
        # Resolve candidate files with git-grep and parse only those while the
        # persistent SQLite/FTS index warms in the background.
        if not self._fully_indexed and (name is not None or path is not None):
            self.start_background_refresh()
            candidate_paths: list[str] = []
            if path is not None:
                candidate_paths = [path.replace("\\", "/")]
            elif name is not None and (self.root / ".git").exists():
                candidate_paths = list(dict.fromkeys(
                    file_path for file_path, _ in self._git_search(
                        name, regex=False, extension=None,
                        path_prefix=None, case_sensitive=True
                    )
                ))
            if candidate_paths:
                output: list[dict] = []
                trusted_root = self.root.resolve()
                for candidate in candidate_paths[:64]:
                    try:
                        file_path = self._normalize_relative_path(candidate)
                        if (self._is_protected_path(file_path) or
                                any(part in SKIP_DIRS for part in Path(file_path).parts)):
                            continue
                        target = self.root / Path(file_path)
                        if (self._has_symlink_component(file_path) or
                                not target.resolve().is_relative_to(trusted_root) or
                                not target.is_file() or
                                target.stat().st_size > MAX_INDEX_BYTES):
                            continue
                        # Cold discovery must not bypass the 2 MiB text limit.
                        # A growing file is rejected before decoding rather
                        # than loaded entirely by Path.read_text().
                        with target.open("rb") as stream:
                            raw = stream.read(MAX_INDEX_BYTES + 1)
                        if len(raw) > MAX_INDEX_BYTES or b"\0" in raw:
                            continue
                        content = raw.decode("utf-8-sig")
                    except (OSError, UnicodeDecodeError, ValueError, RuntimeError):
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
                if output or path is not None:
                    return output

            # Non-Git workspaces have no cheap cold symbol locator.
            if not (self.root / ".git").exists():
                self.refresh(force=True)

        if not self._fully_indexed:
            self.refresh()

        rows = load_rows()
        if not rows and (name is not None or path is not None):
            # The index may be hot while an external tool has just created or
            # changed a source file. Refresh only concrete candidates when
            # possible; fall back to one full refresh for non-Git workspaces.
            if path is not None:
                self.update_path(path)
            elif name is not None and (self.root / ".git").exists():
                live = self._git_search(
                    name, regex=False, extension=None,
                    path_prefix=None, case_sensitive=True
                )
                for file_path in dict.fromkeys(file_path for file_path, _ in live):
                    self.update_path(file_path)
            else:
                self.refresh(force=True)
            rows = load_rows()

        # Old indexes may still contain paths that are excluded by today's
        # scanner. Never expose such rows while an incremental refresh waits.
        with self._lock:
            protected=set(self._protected_keys)
        visible=(row for row in rows
                 if row[0].casefold() not in protected and
                 not any(part in SKIP_DIRS for part in Path(row[0]).parts))
        return [
            {"id": f"S{index}", "file": row[0], "name": row[1], "kind": row[2],
             "line": row[3], "column": row[4], "signature": row[5]}
            for index, row in enumerate(visible, 1)
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
        # -e disambiguates a literal leading hyphen from git grep flags.
        argv += ["-e", query, "--"]
        prefix = self._normalize_relative_path(path_prefix) if path_prefix else ""
        ext = (extension if extension.startswith(".") else "." + extension) if extension else ""
        if prefix:
            # The path prefix is a literal directory or file path, not an
            # attacker-controlled Git pathspec glob. Extension is verified
            # below since separate pathspecs would be unioned, not intersected.
            argv.append(f":(literal){prefix}")
        elif ext:
            argv.append(f":(glob)**/*{ext}")
        try:
            # A huge Git workspace can emit millions of grep hits within the
            # eight-second deadline. Spool output outside the Python heap and
            # avoid presenting a truncated prefix as a complete search.
            with tempfile.TemporaryFile(mode="w+b") as output:
                completed = subprocess.run(
                    argv, cwd=self.root, stdout=output,
                    stderr=subprocess.DEVNULL, timeout=8,
                    check=False, shell=False, **hidden_console_kwargs())
                output.seek(0)
                raw = (completed.stdout if completed.stdout is not None
                       else output.read(MAX_COLD_GIT_BYTES + 1))
            if len(raw) > MAX_COLD_GIT_BYTES:
                return []
            stdout = (raw.decode("utf-8", errors="replace")
                      if isinstance(raw, bytes) else raw)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if completed.returncode not in {0, 1}:
            return []
        rows: list[tuple[str, str]] = []
        # Large cold grep results must not repeatedly resolve the same root.
        trusted_root = self.root.resolve()
        for line in stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            try:
                relative = self._normalize_relative_path(parts[0])
            except ValueError:
                continue
            if any(part in SKIP_DIRS for part in Path(relative).parts):
                continue
            folded=relative.casefold()
            if prefix and not (folded==prefix.casefold() or
                               folded.startswith(prefix.casefold()+"/")):
                continue
            if ext and not folded.endswith(ext.casefold()):
                continue
            # Cold Git discovery must honor the same sensitive-file rules as
            # the SQLite/FTS index: protected files must never be returned.
            with self._lock:
                if self._is_protected_path(relative):
                    continue
            target = self.root / relative
            try:
                if (self._has_symlink_component(relative) or
                    not target.resolve().is_relative_to(trusted_root)):
                    continue
            except (OSError, RuntimeError):
                continue
            rows.append((relative, parts[1] + ":" + parts[2]))
            if len(rows) >= MAX_COLD_GIT_ROWS:
                break
        return rows

    @classmethod
    def _literal_prefix_filter(cls, prefix: str, path_column: str) -> tuple[str, list[str]]:
        # Percent, underscore and the escape character in directory names
        # are literal path bytes, not wildcard operators.
        normalized=cls._normalize_relative_path(prefix)
        escaped=(normalized.casefold().replace("!", "!!")
                 .replace("%", "!%").replace("_", "!_"))
        return (f"(lower({path_column}) = ? OR lower({path_column}) LIKE ? ESCAPE '!')",
                [normalized.casefold(), escaped + "/%"])

    def _candidate_rows(self, query: str, *, regex: bool, extension: str | None,
                        path_prefix: str | None) -> list[tuple[str, str, int, int]]:
        # SQLite lower() only folds ASCII, while Python's final matcher
        # uses Unicode casefold. Non-ASCII queries must not be discarded
        # by a narrower SQL/FTS candidate prefilter.
        if self._fts_enabled and not regex and len(query) >= 3 and query.isascii():
            clauses = ["files_fts MATCH ?"]
            params: list[object] = ['"' + query.replace('"', '""') + '"']
            if extension:
                clauses.append("f.extension = ?")
                params.append(extension if extension.startswith(".") else "." + extension)
            if path_prefix:
                clause, values=self._literal_prefix_filter(path_prefix,"f.path")
                clauses.append(clause)
                params.extend(values)
            sql = """SELECT f.path,f.content,f.size,f.mtime_ns
                     FROM files f JOIN files_fts ON files_fts.path=f.path
                     WHERE """ + " AND ".join(clauses)
            try:
                with self._connect() as db:
                    rows = db.execute(sql, params).fetchall()
                # An imported/stale row may survive another process marking
                # its path protected. Never expose its source text in search.
                return [row for row in rows
                        if not self._is_protected_path(row[0])
                        and not any(part in SKIP_DIRS
                                    for part in Path(row[0]).parts)]
            except sqlite3.OperationalError:
                pass

        clauses = ["content IS NOT NULL"]
        params = []
        if extension:
            clauses.append("extension = ?")
            params.append(extension if extension.startswith(".") else "." + extension)
        if path_prefix:
            clause, values=self._literal_prefix_filter(path_prefix,"path")
            clauses.append(clause)
            params.extend(values)
        if not regex and query.isascii():
            clauses.append("instr(lower(content), ?) > 0")
            params.append(query.casefold())
        with self._connect() as db:
            rows = db.execute(
                "SELECT path,content,size,mtime_ns FROM files WHERE " + " AND ".join(clauses), params
            ).fetchall()
        return [row for row in rows
                if not self._is_protected_path(row[0])
                and not any(part in SKIP_DIRS
                            for part in Path(row[0]).parts)]

    def search(self, query: str, *, regex: bool = False, extension: str | None = None,
               path_prefix: str | None = None, case_sensitive: bool = False,
               offset: int = 0, limit: int = 100) -> list[dict]:
        if (type(query) is not str or not query or len(query) > 512
                or "\x00" in query):
            raise ValueError("INVALID_INDEX_QUERY")
        # Bound paging and reject invalid regexes before Git scans or SQL
        # candidate materialization; reuse the same compiled regex.
        _validate_index_page(offset, limit)
        pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE) if regex else None
        self._sync_protected_paths()
        if not self._fully_indexed:
            cold = self._git_search(query, regex=regex, extension=extension,
                                    path_prefix=path_prefix, case_sensitive=case_sensitive)
            self.start_background_refresh()
            if cold:
                found = []
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
            target = self.root / Path(file_path)
            try:
                if (self._has_symlink_component(file_path) or
                        not target.resolve().is_relative_to(self.root.resolve())):
                    stale_paths.append(file_path)
                    continue
                stat = target.stat()
            except (OSError, RuntimeError):
                stale_paths.append(file_path)
                continue
            if stat.st_size != indexed_size or stat.st_mtime_ns != indexed_mtime:
                stale_paths.append(file_path)
        for file_path in stale_paths:
            try:
                self.update_path(file_path)
            except ValueError as exc:
                if str(exc) != "INVALID_PROTECTED_INDEX_PATH":
                    raise
                # The formerly indexed path may have become a symlink outside
                # the workspace. Remove stale content instead of following it
                # or crashing a normal search request.
                with self._lock, self._connect() as db:
                    db.execute("DELETE FROM files WHERE path=?", (file_path,))
                    db.execute("DELETE FROM symbols WHERE path=?", (file_path,))
                    if self._fts_enabled:
                        db.execute("DELETE FROM files_fts WHERE path=?", (file_path,))
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


    def discover_many(self, terms: list[str], *, limit_per_term: int = 12,
                      definitions_per_term: int = 6, references_per_term: int = 24,
                      extension: str | None = None, path_prefix: str | None = None) -> dict:
        """Resolve many definitions/usages with one cold Git scan or parallel warm-index queries."""
        started = time.perf_counter()
        self._sync_protected_paths()
        clean: list[str] = []
        seen_terms: set[str] = set()
        for value in terms or []:
            term = str(value or "").strip()
            if len(term) > 512 or "\x00" in term:
                raise ValueError("INVALID_INDEX_TERM")
            key = term.casefold()
            if not term or key in seen_terms:
                continue
            seen_terms.add(key)
            clean.append(term)
            if len(clean) >= 32:
                break
        if not clean:
            return {
                "terms": [], "by_term": {}, "impact_files": [], "files": [],
                "engine": "none", "single_pass": True, "duration_ms": 0.0,
            }

        limit_per_term = max(1, min(50, int(limit_per_term)))
        definitions_per_term = max(1, min(20, int(definitions_per_term)))
        references_per_term = max(1, min(100, int(references_per_term)))
        ext = str(extension or "").strip()
        if ext and not ext.startswith("."):
            ext = "." + ext
        # Cold discovery and warm searches must apply the same literal
        # workspace-bound path validation. Stripping "/" or accepting ".."
        # would silently broaden a requested search scope.
        prefix = self._normalize_relative_path(path_prefix) if path_prefix else ""
        by_term: dict[str, dict] = {
            term: {"definitions": [], "references": [], "files": [], "occurrences": 0}
            for term in clean
        }
        file_counts: dict[str, dict[str, object]] = {}
        cold_git = bool(not self._fully_indexed and (self.root / ".git").exists())
        engine = "git-grep" if cold_git else ("sqlite-fts5" if self._fts_enabled else "sqlite-index")

        def term_matches(term: str, text: str) -> bool:
            if re.fullmatch(r"[A-Za-z_$][\w$]*", term):
                return re.search(
                    rf"(?<![\w$]){re.escape(term)}(?![\w$])", text, re.IGNORECASE
                ) is not None
            return term.casefold() in text.casefold()

        def record_reference(term: str, item: dict) -> None:
            bucket = by_term[term]
            bucket["occurrences"] = int(bucket["occurrences"]) + 1
            if len(bucket["references"]) < references_per_term:
                bucket["references"].append(item)
            path = str(item.get("file") or "")
            if not path:
                return
            if path not in bucket["files"]:
                bucket["files"].append(path)
            aggregate = file_counts.setdefault(
                path, {"file": path, "occurrences": 0, "terms": []}
            )
            aggregate["occurrences"] = int(aggregate["occurrences"]) + 1
            if term not in aggregate["terms"]:
                aggregate["terms"].append(term)

        if cold_git:
            argv = ["git", "grep", "-n", "-I", "--untracked", "-i", "-F"]
            for term in clean:
                argv += ["-e", term]
            argv += ["--"]
            if prefix:
                # Match the user-supplied path literally. Treating names with
                # [, ], *, ? as Git pathspec globs drops legitimate files;
                # a concrete file prefix must also work, not only directories.
                # The extension is intersected after Git returns candidates.
                argv.append(f":(literal){prefix}")
            elif ext:
                argv.append(f":(glob)**/*{ext}")
            rows: list[tuple[str, int, str]] = []
            truncated = False

            def bounded_git_grep(command: list[str]) -> tuple[int, str, bool]:
                # capture_output=True buffers arbitrary Git output in RAM.
                # A temporary file lets Git drain safely; only a fixed prefix
                # enters Python, and it is removed when this call returns.
                with tempfile.TemporaryFile(mode="w+b") as output:
                    result = subprocess.run(
                        command, cwd=self.root, stdout=output,
                        stderr=subprocess.DEVNULL, timeout=10, check=False,
                        shell=False, **hidden_console_kwargs(),
                    )
                    output.seek(0)
                    raw = (result.stdout if result.stdout is not None
                           else output.read(MAX_COLD_GIT_BYTES + 1))
                    too_large = len(raw) > MAX_COLD_GIT_BYTES
                    if isinstance(raw, bytes):
                        text = raw[:MAX_COLD_GIT_BYTES].decode("utf-8", errors="replace")
                    else:
                        text = raw[:MAX_COLD_GIT_BYTES]
                    if too_large:
                        # A capped prefix may end mid-line. Never claim that
                        # truncated match as a complete source line.
                        text = text.rsplit("\n", 1)[0]
                    return result.returncode, text, too_large

            try:
                returncode, output_text, truncated = bounded_git_grep(argv)
                # Older Git builds may not support --untracked on grep.
                if returncode not in {0, 1} and "--untracked" in argv:
                    legacy_argv = [value for value in argv if value != "--untracked"]
                    returncode, output_text, truncated = bounded_git_grep(legacy_argv)
                if returncode in {0, 1}:
                    trusted_root = self.root.resolve()
                    for line in output_text.splitlines():
                        parts = line.split(":", 2)
                        if len(parts) != 3:
                            continue
                        try:
                            relative = self._normalize_relative_path(parts[0])
                            number = int(parts[1])
                            if number < 1:
                                continue
                            if any(part in SKIP_DIRS for part in relative.split("/")):
                                continue
                            folded = relative.casefold()
                            if prefix and not (folded == prefix.casefold() or
                                               folded.startswith(prefix.casefold() + "/")):
                                continue
                            if ext and not folded.endswith(ext.casefold()):
                                continue
                            with self._lock:
                                if self._is_protected_path(relative):
                                    continue
                            target = self.root / Path(relative)
                            if (self._has_symlink_component(relative) or
                                    not target.is_file() or
                                    not target.resolve().is_relative_to(trusted_root)):
                                continue
                        except (ValueError, OSError, RuntimeError):
                            continue
                        rows.append((relative, number, parts[2].rstrip()))
                        if len(rows) >= MAX_COLD_GIT_ROWS:
                            truncated = True
                            break
            except (OSError, subprocess.TimeoutExpired):
                rows = []

            wanted_names = {term.casefold(): term for term in clean}
            hard_occurrence_cap = max(references_per_term, limit_per_term * 4)
            for file_path, line_no, line_text in rows:
                matched_terms = [term for term in clean if term_matches(term, line_text)]
                if not matched_terms:
                    continue
                for term in matched_terms:
                    if int(by_term[term]["occurrences"]) < hard_occurrence_cap:
                        record_reference(term, {
                            "file": file_path,
                            "line": line_no,
                            "text": line_text,
                            "score": self._score_match(term, file_path, line_text),
                        })

                for kind, pattern in _SYMBOL_PATTERNS:
                    match = pattern.match(line_text)
                    if not match:
                        continue
                    symbol_name = match.group(1)
                    term = wanted_names.get(symbol_name.casefold())
                    if term is not None and len(by_term[term]["definitions"]) < definitions_per_term:
                        by_term[term]["definitions"].append({
                            "file": file_path,
                            "name": symbol_name,
                            "kind": kind if kind != "python" else "function",
                            "line": line_no,
                            "column": max(1, len(line_text) - len(line_text.lstrip()) + 1),
                            "signature": line_text.strip(),
                        })
                    break
            self.start_background_refresh()
        else:
            if not self._fully_indexed:
                self.refresh()

            def resolve_term(term: str) -> tuple[str, list[dict], list[dict]]:
                # Scope definitions just like references. Limiting before
                # filtering can return an out-of-scope symbol and hide the
                # actual in-scope definition when definitions_per_term is low.
                # Compare normalized relative paths, never Git pathspec globs.
                def in_scope(item: dict) -> bool:
                    relative = str(item.get("file") or "").casefold()
                    if prefix and not (relative == prefix.casefold() or
                                       relative.startswith(prefix.casefold() + "/")):
                        return False
                    if ext and not relative.endswith(ext.casefold()):
                        return False
                    return True

                definitions = [
                    item for item in self.symbols(name=term) if in_scope(item)
                ][:definitions_per_term]
                candidates = self.search(
                    term,
                    extension=ext or None,
                    path_prefix=prefix or None,
                    limit=min(300, max(references_per_term * 3, references_per_term)),
                )
                references = [
                    item for item in candidates
                    if term_matches(term, str(item.get("text") or ""))
                ]
                return term, definitions, references[:references_per_term]

            workers = min(_INDEX_WORKERS, len(clean))
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="easychange-discover"
            ) as pool:
                resolved = list(pool.map(resolve_term, clean))
            for term, definitions, references in resolved:
                by_term[term]["definitions"] = definitions
                for item in references:
                    record_reference(term, item)

        impact_files = list(file_counts.values())
        impact_files.sort(
            key=lambda item: (
                -len(item.get("terms") or []),
                -int(item.get("occurrences") or 0),
                str(item.get("file") or "").casefold(),
            )
        )
        for term in clean:
            bucket = by_term[term]
            refs = list(bucket["references"])
            refs.sort(
                key=lambda item: (
                    -int(item.get("score") or 0),
                    str(item.get("file") or "").casefold(),
                    int(item.get("line") or 0),
                )
            )
            definition_locations = {
                (str(item.get("file") or ""), int(item.get("line") or 0))
                for item in bucket["definitions"]
                if isinstance(item, dict)
            }
            compact_refs = []
            for item in refs:
                if (
                    str(item.get("file") or ""),
                    int(item.get("line") or 0),
                ) in definition_locations:
                    continue
                value = dict(item)
                value.pop("score", None)
                text = str(value.get("text") or "")
                if len(text) > 320:
                    value["text"] = text[:319] + "…"
                    value["text_truncated"] = True
                compact_refs.append(value)
                if len(compact_refs) >= references_per_term:
                    break
            for definition in bucket["definitions"]:
                if isinstance(definition, dict):
                    signature = str(definition.get("signature") or "")
                    if len(signature) > 320:
                        definition["signature"] = signature[:319] + "…"
                        definition["signature_truncated"] = True
            bucket["references"] = compact_refs
            bucket["files"] = list(bucket["files"])[:32]

        return {
            "terms": clean,
            "by_term": by_term,
            "impact_files": impact_files[:64],
            "files": [item["file"] for item in impact_files[:64]],
            "engine": engine,
            "single_pass": cold_git,
            "truncated": truncated if cold_git else False,
            "filters": {"extension": ext or None, "path_prefix": prefix or None},
            "index_state": "READY" if self._fully_indexed else "WARMING",
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }


def _language(extension: str) -> str:
    return {".py": "python", ".cs": "csharp", ".js": "javascript", ".ts": "typescript",
            ".java": "java", ".kt": "kotlin", ".rs": "rust", ".go": "go", ".cpp": "cpp",
            ".c": "c", ".h": "c", ".php": "php", ".rb": "ruby", ".ps1": "powershell",
            ".sh": "shell", ".html": "html", ".css": "css", ".json": "json", ".md": "markdown"}.get(extension, "text")
