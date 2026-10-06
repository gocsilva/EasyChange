from __future__ import annotations

import codecs
import hashlib
import json
import os
from pathlib import Path
from typing import Any


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

        size = self.path.stat().st_size
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        chars = 0
        newlines = 0
        saw_data = False
        last_byte = b""
        try:
            with self.path.open("rb") as handle:
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    saw_data = True
                    last_byte = block[-1:]
                    digest.update(block)
                    newlines += block.count(b"\n")
                    chars += len(decoder.decode(block, final=False))
                chars += len(decoder.decode(b"", final=True))
        except UnicodeDecodeError as exc:
            raise ValueError("HUMAN_TEXT_INVALID_UTF8") from exc

        lines = 0 if not saw_data else newlines + (0 if last_byte == b"\n" else 1)
        return {
            "exists": True,
            "bytes": size,
            "chars": chars,
            "lines": lines,
            "sha256": digest.hexdigest(),
            "updated_at": self.path.stat().st_mtime,
            "max_bytes": self.max_bytes,
            "complete": True,
        }

    def _write_meta(self, meta: dict[str, Any]) -> None:
        temporary = self.meta_path.with_suffix(".json.tmp")
        payload = json.dumps(
            meta,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.meta_path)

    def info(self) -> dict[str, Any]:
        if self.path.exists() and self.meta_path.exists():
            try:
                value = json.loads(self.meta_path.read_text(encoding="utf-8"))
                if (
                    isinstance(value, dict)
                    and int(value.get("bytes") or -1) == self.path.stat().st_size
                    and value.get("sha256")
                ):
                    return {
                        **value,
                        "exists": True,
                        "max_bytes": self.max_bytes,
                        "complete": True,
                    }
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
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
        size = self.path.stat().st_size
        if size > self.max_bytes:
            raise ValueError("HUMAN_TEXT_EXCEEDS_CONFIGURED_LIMIT")
        return self.path.read_text(encoding="utf-8")

    def save(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str):
            raise TypeError("HUMAN_TEXT_STRING_REQUIRED")
        raw = text.encode("utf-8")
        if len(raw) > self.max_bytes:
            raise ValueError(
                f"HUMAN_TEXT_TOO_LARGE:{len(raw)}>{self.max_bytes}"
            )

        temporary = self.path.with_suffix(".txt.tmp")
        self.root.mkdir(parents=True, exist_ok=True)
        with temporary.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

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
            "updated_at": self.path.stat().st_mtime,
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

    def read(self, *, cursor: int = 0, max_bytes: int = 16384) -> dict[str, Any]:
        meta = self.info()
        total = int(meta["bytes"])
        start = max(0, int(cursor or 0))
        if start > total:
            raise ValueError("HUMAN_TEXT_CURSOR_OUT_OF_RANGE")
        limit = max(512, min(256 * 1024, int(max_bytes or 16384)))

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

        self._validate_cursor_boundary(self.path, start, total)
        with self.path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read(min(limit, total - start))

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
