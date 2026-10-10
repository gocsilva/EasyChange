from __future__ import annotations

import codecs
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any


MAX_HUMAN_TEXT_METADATA_BYTES = 65_536


class HumanTextService:
    """Workspace-scoped human text buffer stored outside the Git workspace."""

    DEFAULT_MAX_BYTES = 64 * 1024 * 1024

    def __init__(self, workspace_state_root: Path) -> None:
        self.root = Path(workspace_state_root) / "human-text"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "buffer.txt"
        self.meta_path = self.root / "buffer.meta.json"
        try:
            configured = int(
                os.environ.get("EASYCHANGE_MAX_HUMAN_TEXT_BYTES")
                or self.DEFAULT_MAX_BYTES
            )
        except (TypeError, ValueError):
            configured = self.DEFAULT_MAX_BYTES
        self.max_bytes = max(
            1024 * 1024,
            min(512 * 1024 * 1024, configured),
        )

    def _empty_info(self) -> dict[str, Any]:
        return {
            "exists": False,
            "bytes": 0,
            "chars": 0,
            "lines": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
            "updated_at": None,
            "max_bytes": self.max_bytes,
            "complete": True,
        }

    def _scan_info(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_info()

        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        chars = 0
        newlines = 0
        saw_data = False
        last_byte = b""
        try:
            with self.path.open("rb") as handle:
                opened=os.fstat(handle.fileno())
                if opened.st_size>self.max_bytes:
                    raise ValueError("HUMAN_TEXT_EXCEEDS_CONFIGURED_LIMIT")
                while True:
                    block = handle.read(min(1024*1024,self.max_bytes+1))
                    if not block:
                        break
                    saw_data = True
                    last_byte = block[-1:]
                    digest.update(block)
                    newlines += block.count(b"\n")
                    chars += len(decoder.decode(block, final=False))
                    if handle.tell()>self.max_bytes:
                        raise ValueError("HUMAN_TEXT_EXCEEDS_CONFIGURED_LIMIT")
                chars += len(decoder.decode(b"", final=True))
                stat=os.fstat(handle.fileno())
            # Hash and metadata must refer to one file generation; do not
            # publish old bytes with a replacement file's inode/timestamps.
            try:
                current=self.path.stat()
            except FileNotFoundError as exc:
                raise ValueError("HUMAN_TEXT_FILE_CHANGED_RETRY") from exc
            signature=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
            if signature(opened)!=signature(stat) or signature(stat)!=signature(current):
                raise ValueError("HUMAN_TEXT_FILE_CHANGED_RETRY")
        except UnicodeDecodeError as exc:
            raise ValueError("HUMAN_TEXT_INVALID_UTF8") from exc

        size = stat.st_size
        lines = 0 if not saw_data else newlines + (0 if last_byte == b"\n" else 1)
        return {
            "exists": True,
            "bytes": size,
            "file_mtime_ns": stat.st_mtime_ns,
            "file_inode": stat.st_ino,
            "chars": chars,
            "lines": lines,
            "sha256": digest.hexdigest(),
            "updated_at": stat.st_mtime,
            "file_mtime_ns": stat.st_mtime_ns,
            "file_inode": stat.st_ino,
            "max_bytes": self.max_bytes,
            "complete": True,
        }

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        # Concurrent GUI and MCP writers must never share a temporary name.
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            try:
                handle = os.fdopen(fd, "wb")
            except BaseException:
                # The temporary file is removed below, but its raw descriptor
                # must also be closed if fdopen never takes ownership.
                os.close(fd)
                raise
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_meta(self, meta: dict[str, Any]) -> None:
        payload = json.dumps(
            meta,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._atomic_write(self.meta_path, payload)

    def info(self) -> dict[str, Any]:
        if self.path.exists() and self.meta_path.exists():
            try:
                # The metadata file is tiny in normal operation. A corrupted
                # or externally replaced file must not allocate unbounded RAM
                # before the primary text buffer's size guard runs.
                with self.meta_path.open("rb") as stream:
                    raw = stream.read(MAX_HUMAN_TEXT_METADATA_BYTES + 1)
                if len(raw) > MAX_HUMAN_TEXT_METADATA_BYTES:
                    raise ValueError("HUMAN_TEXT_METADATA_TOO_LARGE")
                value = json.loads(raw.decode("utf-8"))
                file_stat = self.path.stat()
                if (
                    isinstance(value, dict)
                    and value.get("bytes") is not None
                    and int(value["bytes"]) == file_stat.st_size
                    and value.get("file_mtime_ns") == file_stat.st_mtime_ns
                    and value.get("file_inode") == file_stat.st_ino
                    and isinstance(value.get("sha256"), str)
                    and len(value["sha256"]) == 64
                    and all(c in "0123456789abcdef" for c in value["sha256"])
                ):
                    return {
                        **value,
                        "exists": True,
                        "max_bytes": self.max_bytes,
                        "complete": True,
                    }
            except (OSError, ValueError, TypeError, UnicodeError):
                pass

        meta = self._scan_info()
        if meta["exists"]:
            try:
                self._write_meta(meta)
            except OSError:
                pass
        return meta

    def load(self) -> str:
        if not self.path.exists():
            return ""
        # A size check followed by read_text() is vulnerable to a writer
        # atomically replacing the file between those two operations.
        with self.path.open("rb") as handle:
            raw = handle.read(self.max_bytes + 1)
        if len(raw) > self.max_bytes:
            raise ValueError("HUMAN_TEXT_EXCEEDS_CONFIGURED_LIMIT")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("HUMAN_TEXT_INVALID_UTF8") from exc

    def save(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str):
            raise TypeError("HUMAN_TEXT_STRING_REQUIRED")
        # Even ASCII cannot fit once the code-point count exceeds the byte
        # budget. Reject before creating another very large UTF-8 allocation.
        if len(text) > self.max_bytes:
            raise ValueError("HUMAN_TEXT_TOO_LARGE")
        raw = text.encode("utf-8")
        if len(raw) > self.max_bytes:
            raise ValueError(
                f"HUMAN_TEXT_TOO_LARGE:{len(raw)}>{self.max_bytes}"
            )

        self._atomic_write(self.path, raw)
        stat = self.path.stat()

        meta = {
            "exists": True,
            "bytes": len(raw),
            "chars": len(text),
            "lines": (
                0
                if not text
                else text.count("\n") + (0 if text.endswith("\n") else 1)
            ),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "updated_at": stat.st_mtime,
            "file_mtime_ns": stat.st_mtime_ns,
            "file_inode": stat.st_ino,
            "max_bytes": self.max_bytes,
            "complete": True,
        }
        self._write_meta(meta)
        return meta

    def clear(self) -> dict[str, Any]:
        return self.save("")

    @staticmethod
    def _validate_cursor_boundary(path: Path, cursor: int, total: int) -> None:
        if cursor <= 0 or cursor >= total:
            return
        with path.open("rb") as handle:
            handle.seek(cursor)
            current = handle.read(1)
        if current and (current[0] & 0xC0) == 0x80:
            raise ValueError("HUMAN_TEXT_CURSOR_NOT_UTF8_BOUNDARY")

    @staticmethod
    def _integer_option(value: Any, *, default: int, error: str) -> int:
        if value is None:
            return default
        if type(value) is int:
            parsed = value
        elif isinstance(value, str):
            token = value.strip()
            if (not 1 <= len(token) <= 32 or
                    not token.lstrip("+-").isascii() or
                    not token.lstrip("+-").isdecimal()):
                raise ValueError(error)
            try:
                parsed = int(token, 10)
            except ValueError as exc:
                raise ValueError(error) from exc
        elif type(value) is float:
            if (not math.isfinite(value) or not value.is_integer() or
                    abs(value) > 2**53):
                raise ValueError(error)
            parsed = int(value)
        else:
            raise ValueError(error)
        if abs(parsed) > 2**63 - 1:
            raise ValueError(error)
        return parsed

    def read(self, *, cursor: int = 0, max_bytes: int = 16384) -> dict[str, Any]:
        # Reject invalid user-controlled cursors before scanning a potentially
        # large buffer for metadata, and never float-round a huge page index.
        start = max(0, self._integer_option(
            cursor, default=0, error="HUMAN_TEXT_INVALID_CURSOR"))
        requested = self._integer_option(
            max_bytes, default=16384, error="HUMAN_TEXT_INVALID_PAGE_SIZE")
        limit = max(512, min(256 * 1024, requested or 16384))
        meta = self.info()
        total = int(meta["bytes"])
        if start > total:
            raise ValueError("HUMAN_TEXT_CURSOR_OUT_OF_RANGE")

        # A concurrent writer can atomically replace the file after info().
        # Read from one opened generation and compare it with the metadata
        # before returning bytes, including an empty terminal page.
        raw=b""
        if meta.get("exists"):
            try:
                with self.path.open("rb") as handle:
                    opened=os.fstat(handle.fileno())
                    if (opened.st_size != total or
                            opened.st_ino != meta.get("file_inode") or
                            opened.st_mtime_ns != meta.get("file_mtime_ns")):
                        raise ValueError("HUMAN_TEXT_FILE_CHANGED_RETRY")
                    if 0 < start < total:
                        handle.seek(start)
                        following=handle.read(1)
                        if following and (following[0] & 0xC0) == 0x80:
                            raise ValueError("HUMAN_TEXT_CURSOR_NOT_UTF8_BOUNDARY")
                    if start < total:
                        handle.seek(start)
                        raw=handle.read(min(limit,total-start))
            except FileNotFoundError as exc:
                raise ValueError("HUMAN_TEXT_FILE_CHANGED_RETRY") from exc
        elif total:
            raise ValueError("HUMAN_TEXT_FILE_CHANGED_RETRY")

        if total == 0 or start == total:
            return {
                **meta,
                "cursor": start,
                "next_cursor": None,
                "returned_bytes": 0,
                "remaining_bytes": 0,
                "text": "",
                "complete": True,
            }

        payload = raw
        while payload:
            try:
                text = payload.decode("utf-8")
                break
            except UnicodeDecodeError as exc:
                if exc.reason != "unexpected end of data":
                    raise ValueError("HUMAN_TEXT_INVALID_UTF8") from exc
                payload = payload[:-1]
        else:
            text = ""

        consumed = len(payload)
        if consumed == 0 and raw:
            raise ValueError("HUMAN_TEXT_INVALID_UTF8")

        next_cursor = start + consumed
        complete = next_cursor >= total
        return {
            **meta,
            "cursor": start,
            "next_cursor": None if complete else next_cursor,
            "returned_bytes": consumed,
            "remaining_bytes": max(0, total - next_cursor),
            "text": text,
            "complete": complete,
        }
