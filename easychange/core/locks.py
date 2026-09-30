from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from contextlib import contextmanager


@dataclass(slots=True)
class FileLock:
    path: str
    owner: str
    session: str
    expires_at: float | None = None


class LockManager:
    """Cross-thread/process file locks stored transactionally in SQLite."""
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.db_path = path.with_suffix(".db") if path else None
        if self.db_path:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.execute("CREATE TABLE IF NOT EXISTS locks(path TEXT PRIMARY KEY, owner TEXT, session TEXT, expires_at REAL)")

    @contextmanager
    def _connect(self):
        if self.db_path is None: raise RuntimeError("Lock storage is not configured")
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            db.execute("PRAGMA busy_timeout=10000")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def acquire(self, path: str, owner: str, session: str, ttl_seconds: int | None = None) -> FileLock:
        expiry = time.time() + (ttl_seconds or 3600)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM locks WHERE expires_at IS NOT NULL AND expires_at <= ?", (time.time(),))
            row = db.execute("SELECT owner,session,expires_at FROM locks WHERE path=?", (path,)).fetchone()
            if row and (row[0] != owner or row[1] != session):
                raise BlockingIOError(f"LOCKED: {path} owned by {row[0]}")
            db.execute("INSERT OR REPLACE INTO locks VALUES(?,?,?,?)", (path, owner, session, expiry))
        return FileLock(path, owner, session, expiry)

    def release(self, path: str, owner: str, session: str) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner,session FROM locks WHERE path=?", (path,)).fetchone()
            if not row: return False
            if row != (owner, session): raise PermissionError(f"LOCKED: {path} owned by {row[0]}")
            db.execute("DELETE FROM locks WHERE path=?", (path,))
            return True

    def check_write(self, path: str, owner: str, session: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM locks WHERE expires_at IS NOT NULL AND expires_at <= ?", (time.time(),))
            row = db.execute("SELECT owner,session FROM locks WHERE path=?", (path,)).fetchone()
            if row and row != (owner, session): raise BlockingIOError(f"LOCKED: {path} owned by {row[0]}")

    def list(self) -> list[dict]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM locks WHERE expires_at IS NOT NULL AND expires_at <= ?", (time.time(),))
            rows = db.execute("SELECT path,owner,session,expires_at FROM locks ORDER BY path").fetchall()
        return [asdict(FileLock(*row)) for row in rows]

    def release_owner(self, owner: str, session: str) -> int:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("DELETE FROM locks WHERE owner=? AND session=?", (owner, session))
            return cursor.rowcount
