from __future__ import annotations
import os
import stat
import subprocess
from .subprocess_utils import hidden_console_kwargs

import difflib
import hashlib
import mimetypes
import tempfile
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
        self._read_cache: OrderedDict[str, tuple[int, int, int, int, str, list[str]]] = OrderedDict()
        self._read_cache_bytes = 0
        self._read_cache_max_entries = 32
        self._read_cache_max_bytes = 16 * 1024 * 1024

    @property
    def baselines(self) -> dict[str, str]:
        with self._guard: return dict(self._baselines)

    def _read_cache_get(self, relative: str, size: int, mtime_ns: int,
                        device: int, inode: int):
        with self._guard:
            cached = self._read_cache.get(relative)
            if cached is None:
                return None
            cached_size, cached_mtime, cached_dev, cached_ino, digest, lines = cached
            if (cached_size != size or cached_mtime != mtime_ns
                    or cached_dev != device or cached_ino != inode):
                self._read_cache.pop(relative, None)
                self._read_cache_bytes = max(0, self._read_cache_bytes - cached_size)
                return None
            self._read_cache.move_to_end(relative)
            return digest, lines

    def _read_cache_put(self, relative: str, size: int, mtime_ns: int,
                        digest: str, lines: list[str], *,
                        device: int, inode: int) -> None:
        if size > self._read_cache_max_bytes:
            return
        with self._guard:
            previous = self._read_cache.pop(relative, None)
            if previous is not None:
                self._read_cache_bytes = max(0, self._read_cache_bytes - previous[0])
            self._read_cache[relative] = (size, mtime_ns, device, inode, digest, lines)
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
        limit = max(1, int(limit))
        ignored = {
            ".git", ".easychange", ".venv", "venv", "node_modules", "bin", "obj",
            "dist", "build", "__pycache__", ".gradle", ".idea", ".vs",
            ".pytest_cache", ".mypy_cache", ".ruff_cache",
        }
        root = self.workspace.root_path
        items: list[dict[str, str]] = []

        def append_relative(relative: str) -> bool:
            normalized = str(relative or "").replace("\\", "/").strip("/")
            if not normalized:
                return False
            parts = tuple(part for part in normalized.split("/") if part)
            if any(part.casefold() in ignored for part in parts):
                return False
            candidate = root / Path(normalized)
            try:
                # Git inventory and filesystem fallback must not publish
                # symlink/device or hardlinked external aliases as editable
                # workspace files; the caller can subsequently read paths.
                metadata = candidate.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    return False
                candidate.resolve(strict=True).relative_to(root)
            except (OSError, ValueError):
                return False
            items.append({
                "path": normalized,
                "id": f"F{len(items) + 1}",
                "kind": "file",
            })
            return len(items) >= limit

        # Git already maintains a fast path index and also knows untracked files
        # through --others/--exclude-standard. Avoid materializing/sorting the
        # whole workspace tree before applying the caller's limit.
        if (root / ".git").exists():
            try:
                completed = subprocess.run(
                    [
                        "git", "ls-files", "--cached", "--others",
                        "--exclude-standard", "-z",
                    ],
                    cwd=str(root),
                    capture_output=True,
                    timeout=3.0,
                    check=False,
                    **hidden_console_kwargs(),
                )
                if completed.returncode == 0:
                    for raw in completed.stdout.split(b"\0"):
                        if not raw:
                            continue
                        relative = raw.decode("utf-8", errors="replace")
                        if append_relative(relative):
                            break
                    if items:
                        return items
            except (OSError, subprocess.SubprocessError):
                pass

        # Non-Git/fallback path: prune ignored directories before descending and
        # stop as soon as the requested limit is satisfied. Sort only each local
        # directory, never the entire recursive tree.
        for current_root, dirs, files in os.walk(root):
            dirs[:] = sorted(
                name for name in dirs
                if name.casefold() not in ignored
            )
            current = Path(current_root)
            for name in sorted(files):
                try:
                    relative = (current / name).relative_to(root).as_posix()
                except ValueError:
                    continue
                if append_relative(relative):
                    return items
        return items

    @staticmethod
    def _read_limited_text(path: Path) -> bytes:
        # A stat-before-read check is not a memory limit: the file may grow
        # concurrently. Always cap the actual file descriptor read as well.
        with path.open("rb") as stream:
            return stream.read(MAX_TEXT_FILE_BYTES + 1)

    def read(self, path: str, start: int = 1, count: int = 120) -> dict:
        target = self.workspace.resolve(path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(str(target))
        relative = target.relative_to(self.workspace.root_path).as_posix()
        stat = target.stat()
        if stat.st_size > MAX_TEXT_FILE_BYTES:
            raise ValueError(f"File exceeds text read limit ({MAX_TEXT_FILE_BYTES} bytes)")
        cached = self._read_cache_get(
            relative,stat.st_size,stat.st_mtime_ns,stat.st_dev,stat.st_ino)
        cache_hit = cached is not None
        if cached is None:
            # An external editor can atomically replace a file between our
            # original stat and descriptor read. Never cache old bytes using
            # the new file's size/mtime/inode: that would make subsequent EC1
            # reads report stale source and an incorrect edit baseline.
            for attempt in range(2):
                before = stat if attempt == 0 else target.stat()
                if before.st_size > MAX_TEXT_FILE_BYTES:
                    raise ValueError(f"File exceeds text read limit ({MAX_TEXT_FILE_BYTES} bytes)")
                data = self._read_limited_text(target)
                post = target.stat()
                stamp = lambda value: (
                    value.st_size, value.st_mtime_ns, value.st_dev, value.st_ino
                )
                if stamp(before) != stamp(post):
                    if attempt == 0:
                        continue
                    raise OSError("FILE_CHANGED_DURING_READ")
                break
            if b"\0" in data:
                raise ValueError("Binary files cannot be read as text")
            if len(data) > MAX_TEXT_FILE_BYTES:
                raise ValueError(f"File exceeds text read limit ({MAX_TEXT_FILE_BYTES} bytes)")
            lines = data.decode("utf-8-sig").splitlines()
            digest = hashlib.sha256(data).hexdigest()
            self._read_cache_put(
                relative,post.st_size,post.st_mtime_ns,digest,lines,
                device=post.st_dev,inode=post.st_ino)
        else:
            digest, lines = cached
        with self._guard:
            self._baselines[relative] = digest
        start = max(1, int(start))
        requested_count = max(1, int(count))
        chunk = lines[start - 1:start - 1 + requested_count]
        returned_lines = len(chunk)
        has_more = start - 1 + returned_lines < len(lines)
        return {
            "path": relative,
            "start": start,
            "total_lines": len(lines),
            "hash": digest[:12],
            "cache_hit": cache_hit,
            "lines": [f"{i}|{line}" for i, line in enumerate(chunk, start)],
            "requested_count": requested_count,
            "returned_lines": returned_lines,
            "content_bytes": sum(len(line.encode("utf-8")) for line in chunk),
            "has_more": has_more,
            "next_start": start + returned_lines if has_more else None,
            "previous_start": max(1, start - requested_count) if start > 1 else None,
        }

    def _read_for_edit(self, path: str) -> tuple[Path, str, str]:
        """Read one editable text snapshot and establish/verify its baseline."""
        target = self.workspace.resolve(path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(str(target))
        data = self._read_limited_text(target)
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
    @staticmethod
    def _atomic_write_bytes(target: Path, payload: bytes) -> None:
        # Thread identifiers may repeat across multiple EasyChange processes.
        # A kernel-assigned unique staging name prevents cross-process clobber.
        stage = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",dir=target.parent,prefix=f".{target.name}.easychange-",
                suffix=".tmp",delete=False
            ) as stream:
                stage=Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            stage.replace(target)
        finally:
            if stage is not None:
                stage.unlink(missing_ok=True)

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
            previous_bytes = self._read_limited_text(target)
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
        self._atomic_write_bytes(target,new_bytes)

        # Do not make the caller issue a read-after-write just to know whether
        # the mutation committed. Verify the exact bytes before returning.
        actual_bytes = self._read_limited_text(target)
        actual_after_hash = hashlib.sha256(actual_bytes).hexdigest()
        if actual_after_hash != expected_after_hash:
            # Another process may have updated the target between our atomic
            # replace and this verification. Never overwrite or delete its
            # changes while handling a failed integrity check.
            raise IOError("POST_WRITE_HASH_MISMATCH")

        self._opened[path] = target
        with self._guard:
            self._baselines[relative] = actual_after_hash
        written_stat = target.stat()
        self._read_cache_put(
            relative, written_stat.st_size, written_stat.st_mtime_ns,
            actual_after_hash, content.splitlines(),
            device=written_stat.st_dev,inode=written_stat.st_ino,
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
        if not isinstance(old, str) or not old:
            raise ValueError("Search text must be a nonempty string")
        if type(count) is not int or count < 0:
            raise ValueError("Replacement count must be a nonnegative integer")
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
        previous_line = lines[line - 1]
        ending = ("\r\n" if previous_line.endswith("\r\n")
                  else "\n" if previous_line.endswith("\n")
                  else "\r" if previous_line.endswith("\r") else "")
        lines[line - 1] = value + ending
        updated = "".join(lines)
        self.write(path, updated)
        return {"path": relative, "line": line, "_before": original, "_after": updated}

    def append(self, path: str, value: str) -> dict:
        target = self.workspace.resolve(path)
        before = ""
        if target.exists():
            # Append is read-modify-write. Establish an optimistic baseline
            # before computing new content, or another local editor can
            # change the file between this read and write(), and the append
            # silently overwrites their update.
            _, _, before = self._read_for_edit(path)
        separator = "" if not before or before.endswith(("\n", "\r")) else "\n"
        self.write(path, before + separator + value)
        return {"path": path, "appended_chars": len(value)}

    @staticmethod
    def _edit_line_ending(lines: list[str], index: int) -> str:
        # Prefer the nearest original line's terminator; preserve mixed-EOL
        # neighbors byte-for-byte instead of rebuilding the whole file.
        for i in sorted(range(len(lines)), key=lambda i: abs(i - index)):
            item = lines[i]
            if item.endswith("\r\n"):
                return "\r\n"
            if item.endswith("\n"):
                return "\n"
            if item.endswith("\r"):
                return "\r"
        return "\n"

    def insert_line(self, path: str, line: int, value: str) -> dict:
        _, relative, original = self._read_for_edit(path)
        lines = original.splitlines(keepends=True)
        if line < 1 or line > len(lines) + 1:
            raise IndexError(f"Line out of range: {line}")
        index = line - 1
        ending = self._edit_line_ending(lines, index)
        if index == len(lines) and lines and not lines[-1].endswith(("\r", "\n")):
            # Appending after an unterminated last line adds a separator but
            # never invents a final trailing newline that did not exist.
            lines[-1] += ending
            lines.append(value)
        else:
            lines.insert(index, value + ending if lines else value)
        updated = "".join(lines)
        self.write(path, updated)
        return {"path": relative, "line": line, "_before": original, "_after": updated}

    def replace_range(self, path: str, start: int, end: int, value: str) -> dict:
        _, relative, original = self._read_for_edit(path)
        lines = original.splitlines(keepends=True)
        if start < 1 or end < start or end > len(lines):
            raise IndexError("Invalid line range")
        ending = self._edit_line_ending(lines, start - 1)
        removed_last = lines[end - 1]
        had_trailing_ending = removed_last.endswith(("\r", "\n"))
        replacement = [item + ending for item in value.splitlines()]
        if replacement and not had_trailing_ending:
            replacement[-1] = replacement[-1][:-len(ending)]
        lines[start - 1:end] = replacement
        updated = "".join(lines)
        self.write(path, updated)
        return {
            "path": relative, "start": start, "end": end,
            "_before": original, "_after": updated,
        }

    def info(self, path: str) -> dict:
        target = self.workspace.resolve(path, must_exist=True)
        stat = target.stat()
        raw = self._read_limited_text(target) if stat.st_size <= MAX_TEXT_FILE_BYTES else None
        # A file may grow between stat() and the bounded descriptor read.
        if raw is not None and len(raw)>MAX_TEXT_FILE_BYTES:
            raw = None
            stat = target.stat()
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

    @staticmethod
    def _hash_file_stream(path: Path) -> str:
        digest=hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b""):
                digest.update(chunk)
        return digest.hexdigest()

    def hash(self, path: str) -> str:
        return self._hash_file_stream(self.workspace.resolve(path,must_exist=True))

    def delete(self, path: str) -> None:
        target = self.workspace.resolve(path, must_exist=True)
        relative = target.relative_to(self.workspace.root_path).as_posix()
        actual = self._hash_file_stream(target)
        expected = self._baselines.get(relative)
        if expected and expected != actual: raise ExternalChangeError(relative, expected, actual)
        target.unlink()
        with self._guard: self._baselines.pop(relative, None)
        self._read_cache_drop(relative)

    def forget(self, path: str) -> None:
        relative = self.workspace.resolve(path).relative_to(self.workspace.root_path).as_posix()
        with self._guard: self._baselines.pop(relative, None)
        self._read_cache_drop(relative)

    def remember(self, path: str, *, replace: bool = False,
                 snapshot_bytes: bytes | None = None) -> str:
        target = self.workspace.resolve(path, must_exist=True)
        if snapshot_bytes is not None and type(snapshot_bytes) is not bytes:
            raise TypeError("EDIT_BASELINE_SNAPSHOT_MUST_BE_BYTES")
        # GUI preview has already read the editor buffer. Hash those *same*
        # bytes, not a second file read that could observe a concurrent edit
        # and mistakenly authorize saving stale editor content over new work.
        digest = (hashlib.sha256(snapshot_bytes).hexdigest()
                  if snapshot_bytes is not None else self._hash_file_stream(target))
        relative = target.relative_to(self.workspace.root_path).as_posix()
        with self._guard:
            if replace or relative not in self._baselines: self._baselines[relative] = digest
        return relative
