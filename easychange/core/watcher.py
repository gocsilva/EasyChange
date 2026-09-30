from __future__ import annotations

import threading
from collections.abc import Callable

from .indexer import Indexer


class WorkspaceWatcher:
    """Incremental workspace watcher optimized for Git code workspaces.

    Git repositories avoid repository-wide Python scans: only dirty, untracked,
    reverted, or HEAD-changed paths are fingerprinted and reindexed. Non-Git
    workspaces retain the correctness-first full refresh fallback.
    """

    def __init__(self, indexer: Indexer, interval: float = 1.5,
                 on_change: Callable[[dict], None] | None = None) -> None:
        self.indexer = indexer
        self.interval = max(0.2, interval)
        self.on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_change: dict | None = None
        self.last_error: str | None = None
        self._fingerprints: dict[str, tuple[int, int] | None] = {}
        self._last_head: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="easychange-watcher", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _git(self, argv: list[str]) -> bytes:
        import subprocess

        completed = subprocess.run(
            ["git", *argv],
            cwd=self.indexer.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
            shell=False,
        )
        if completed.returncode not in {0, 1}:
            return b""
        return completed.stdout or b""

    def _git_paths(self, argv: list[str]) -> set[str]:
        raw = self._git(argv)
        return {
            item.decode("utf-8", errors="replace").replace("\\", "/")
            for item in raw.split(b"\0")
            if item
        }

    def _head(self) -> str | None:
        raw = self._git(["rev-parse", "HEAD"]).strip()
        return raw.decode("ascii", errors="ignore") or None

    def _fingerprint(self, relative: str) -> tuple[int, int] | None:
        target = self.indexer.root / relative
        try:
            stat = target.stat()
        except OSError:
            return None
        return stat.st_size, stat.st_mtime_ns

    def _poll_git(self) -> dict:
        """Poll Git with one status process in the common no-HEAD-change case."""
        raw = self._git([
            "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all",
        ])
        records = raw.split(b"\0")
        dirty: set[str] = set()
        head: str | None = None
        index = 0

        while index < len(records):
            raw_record = records[index]
            index += 1
            if not raw_record:
                continue
            record = raw_record.decode("utf-8", errors="replace")

            if record.startswith("# branch.oid "):
                value = record[len("# branch.oid "):].strip()
                head = None if value == "(initial)" else value
                continue
            if record.startswith("? "):
                dirty.add(record[2:].replace("\\", "/"))
                continue
            if record.startswith("1 "):
                parts = record.split(" ", 8)
                if len(parts) == 9:
                    dirty.add(parts[8].replace("\\", "/"))
                continue
            if record.startswith("u "):
                parts = record.split(" ", 10)
                if len(parts) >= 11:
                    dirty.add(parts[-1].replace("\\", "/"))
                continue
            if record.startswith("2 "):
                # Porcelain v2 -z stores the destination path in this record
                # and the original path as the next NUL-delimited field.
                parts = record.split(" ", 9)
                if len(parts) == 10:
                    dirty.add(parts[9].replace("\\", "/"))
                if index < len(records) and records[index]:
                    original = records[index].decode("utf-8", errors="replace")
                    dirty.add(original.replace("\\", "/"))
                    index += 1

        affected: set[str] = set()
        sentinel = object()
        for relative in dirty:
            fingerprint = self._fingerprint(relative)
            if self._fingerprints.get(relative, sentinel) != fingerprint:
                affected.add(relative)

        # A path disappearing from status was reverted, committed or removed
        # from the worktree. Reindex it once to restore the tracked contents.
        affected.update(set(self._fingerprints) - dirty)

        head_changed = bool(self._last_head and head and head != self._last_head)
        if head_changed:
            affected.update(
                self._git_paths([
                    "diff", "--name-only", "-z",
                    self._last_head, head, "--",
                ])
            )

        indexed = 0
        removed = 0
        for relative in sorted(affected):
            outcome = self.indexer.update_path(relative)
            if outcome.get("removed"):
                removed += 1
            elif outcome.get("updated"):
                indexed += 1

        self._fingerprints = {
            relative: self._fingerprint(relative) for relative in dirty
        }
        self._last_head = head
        return {
            "indexed": indexed,
            "removed": removed,
            "files": len(affected),
            "cached": not bool(affected),
            "backend": "git-incremental",
            "status_backend": "porcelain-v2",
            "git_processes": 1 + int(head_changed),
        }

    def poll_once(self) -> dict:
        if (self.indexer.root / ".git").exists():
            result = self._poll_git()
        else:
            result = self.indexer.refresh(force=True)
            result["backend"] = "filesystem-scan"

        if result["indexed"] or result["removed"]:
            self.last_change = result
            if self.on_change:
                self.on_change(result)
        return result

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll_once()
                self.last_error = None
            except (OSError, RuntimeError):
                import traceback
                self.last_error = traceback.format_exc(limit=1).strip()
