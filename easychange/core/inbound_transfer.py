"""Durable, integrity-checked staging for large inbound EasyChange commands."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import zlib
from pathlib import Path
from typing import Any


class InboundTransferError(RuntimeError):
    def __init__(self, code: str, message: str | None = None, *, data: dict[str, Any] | None = None) -> None:
        super().__init__(message or code)
        self.code = str(code)
        self.data = dict(data or {})


class InboundCommandStore:
    """Stage compressed command bytes in small idempotent HID-friendly chunks."""

    MAX_RAW_BYTES = 2 * 1024 * 1024
    MAX_COMPRESSED_BYTES = MAX_RAW_BYTES + 64 * 1024
    MAX_CHUNKS = 1024
    MAX_CHUNK_BYTES = 8 * 1024
    MAX_TRANSFERS = 32
    TTL_SECONDS = 60 * 60

    _TRANSFER_RE = re.compile(r"^U[A-Z0-9_-]{7,63}$")
    _SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
    _CRC_RE = re.compile(r"^[0-9a-fA-F]{8}$")
    _TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @classmethod
    def _transfer_id(cls, value: str) -> str:
        transfer_id = str(value or "").upper()
        if not cls._TRANSFER_RE.fullmatch(transfer_id):
            raise InboundTransferError("INVALID_INBOUND_TRANSFER_ID")
        return transfer_id

    def _dir(self, transfer_id: str) -> Path:
        return self.root / self._transfer_id(transfer_id)

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Each writer needs an isolated staging file even when two EasyChange
        # processes receive a retransmission for the same transfer index.
        fd, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _meta_path(self, transfer_id: str) -> Path:
        return self._dir(transfer_id) / "meta.json"

    def _load_meta_locked(self, transfer_id: str) -> dict[str, Any]:
        path = self._meta_path(transfer_id)
        if not path.exists():
            raise InboundTransferError(
                "INBOUND_TRANSFER_NOT_FOUND",
                data={"transfer_id": self._transfer_id(transfer_id)},
            )
        try:
            with path.open("rb") as stream:
                raw = stream.read(16 * 1024 + 1)
            if len(raw) > 16 * 1024:
                raise ValueError("INBOUND_META_TOO_LARGE")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("INBOUND_META_NOT_OBJECT")
            if (value.get("version") != 1
                    or value.get("transfer_id") != transfer_id
                    or value.get("codec") != "zlib"
                    or type(value.get("chunk_count")) is not int
                    or not 1 <= value["chunk_count"] <= self.MAX_CHUNKS
                    or type(value.get("raw_bytes")) is not int
                    or not 1 <= value["raw_bytes"] <= self.MAX_RAW_BYTES
                    or type(value.get("compressed_bytes")) is not int
                    or not 1 <= value["compressed_bytes"] <= self.MAX_COMPRESSED_BYTES
                    or not isinstance(value.get("sha256"), str)
                    or not self._SHA_RE.fullmatch(value["sha256"])):
                raise ValueError("INBOUND_META_FIELDS_INVALID")
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise InboundTransferError("INBOUND_TRANSFER_META_CORRUPT") from exc
        return value

    def _cleanup_locked(self) -> None:
        now = time.time()
        try:
            directories = [
                path for path in self.root.iterdir()
                if path.is_dir() and not path.is_symlink()
            ]
        except OSError:
            return
        ranked: list[tuple[float, Path]] = []
        for path in directories:
            try:
                ranked.append((float(path.stat().st_mtime), path))
            except OSError:
                continue
        ranked.sort(reverse=True)
        for index, (mtime, path) in enumerate(ranked):
            if index >= self.MAX_TRANSFERS or now - mtime > self.TTL_SECONDS:
                shutil.rmtree(path, ignore_errors=True)

    def begin(
        self,
        transfer_id: str,
        *,
        raw_bytes: int,
        compressed_bytes: int,
        chunk_count: int,
        sha256: str,
    ) -> dict[str, Any]:
        transfer_id = self._transfer_id(transfer_id)
        raw_bytes = int(raw_bytes)
        compressed_bytes = int(compressed_bytes)
        chunk_count = int(chunk_count)
        sha256 = str(sha256 or "").casefold()
        if raw_bytes <= 0 or raw_bytes > self.MAX_RAW_BYTES:
            raise InboundTransferError(
                "INBOUND_RAW_SIZE_LIMIT",
                data={"raw_bytes": raw_bytes, "max_raw_bytes": self.MAX_RAW_BYTES},
            )
        if compressed_bytes <= 0 or compressed_bytes > self.MAX_COMPRESSED_BYTES:
            raise InboundTransferError(
                "INBOUND_COMPRESSED_SIZE_LIMIT",
                data={
                    "compressed_bytes": compressed_bytes,
                    "max_compressed_bytes": self.MAX_COMPRESSED_BYTES,
                },
            )
        if chunk_count <= 0 or chunk_count > self.MAX_CHUNKS:
            raise InboundTransferError(
                "INBOUND_CHUNK_COUNT_LIMIT",
                data={"chunk_count": chunk_count, "max_chunks": self.MAX_CHUNKS},
            )
        if not self._SHA_RE.fullmatch(sha256):
            raise InboundTransferError("INVALID_INBOUND_SHA256")

        expected = {
            "version": 1,
            "transfer_id": transfer_id,
            "codec": "zlib",
            "raw_bytes": raw_bytes,
            "compressed_bytes": compressed_bytes,
            "chunk_count": chunk_count,
            "sha256": sha256,
        }
        with self._lock:
            self._cleanup_locked()
            directory = self._dir(transfer_id)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "meta.json"
            if path.exists():
                current = self._load_meta_locked(transfer_id)
                comparable = {key: current.get(key) for key in expected}
                if comparable != expected:
                    raise InboundTransferError(
                        "INBOUND_TRANSFER_CONFLICT",
                        data={"transfer_id": transfer_id},
                    )
                os.utime(directory, None)
                return {**expected, "duplicate": True}

            meta = {**expected, "created_at": time.time()}
            self._atomic_write(
                path,
                json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            )
            os.utime(directory, None)
            return {**expected, "duplicate": False}

    def put_chunk(
        self,
        transfer_id: str,
        *,
        index: int,
        crc32: str,
        token: str,
    ) -> dict[str, Any]:
        transfer_id = self._transfer_id(transfer_id)
        index = int(index)
        crc32 = str(crc32 or "").casefold()
        token = str(token or "")
        if not self._CRC_RE.fullmatch(crc32):
            raise InboundTransferError("INVALID_INBOUND_CHUNK_CRC")
        # An 8 KiB chunk cannot require an unbounded Base64 input.
        # Reject before regex validation and decoding to avoid large copies.
        max_token_chars = 4 * ((self.MAX_CHUNK_BYTES + 2) // 3) + 4
        if len(token) > max_token_chars:
            raise InboundTransferError(
                "INBOUND_CHUNK_SIZE_LIMIT",
                data={"token_chars": len(token),
                      "max_token_chars": max_token_chars})
        if not token or not self._TOKEN_RE.fullmatch(token):
            raise InboundTransferError("INVALID_INBOUND_CHUNK_BASE64URL")
        try:
            payload = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        except (ValueError, binascii.Error) as exc:
            raise InboundTransferError("INVALID_INBOUND_CHUNK_BASE64URL") from exc
        if not payload or len(payload) > self.MAX_CHUNK_BYTES:
            raise InboundTransferError(
                "INBOUND_CHUNK_SIZE_LIMIT",
                data={"chunk_bytes": len(payload), "max_chunk_bytes": self.MAX_CHUNK_BYTES},
            )
        actual_crc = f"{binascii.crc32(payload) & 0xffffffff:08x}"
        if actual_crc != crc32:
            raise InboundTransferError(
                "INBOUND_CHUNK_CRC_MISMATCH",
                data={"index": index, "expected_crc32": crc32, "actual_crc32": actual_crc},
            )

        with self._lock:
            meta = self._load_meta_locked(transfer_id)
            count = int(meta.get("chunk_count") or 0)
            if index < 0 or index >= count:
                raise InboundTransferError(
                    "INBOUND_CHUNK_INDEX_OUT_OF_RANGE",
                    data={"index": index, "chunk_count": count},
                )
            directory = self._dir(transfer_id)
            target = directory / f"{index:04d}.part"
            if target.exists():
                try:
                    previous = target.read_bytes()
                except OSError as exc:
                    raise InboundTransferError("INBOUND_CHUNK_READ_FAILED") from exc
                if previous != payload:
                    raise InboundTransferError(
                        "INBOUND_CHUNK_CONFLICT",
                        data={"index": index},
                    )
                os.utime(directory, None)
                return {
                    "transfer_id": transfer_id,
                    "index": index,
                    "chunk_bytes": len(payload),
                    "duplicate": True,
                }
            self._atomic_write(target, payload)
            os.utime(directory, None)
            return {
                "transfer_id": transfer_id,
                "index": index,
                "chunk_bytes": len(payload),
                "duplicate": False,
            }

    def _missing_locked(self, transfer_id: str, chunk_count: int) -> list[int]:
        directory = self._dir(transfer_id)
        return [
            index
            for index in range(int(chunk_count))
            if not (directory / f"{index:04d}.part").is_file()
        ]

    def _assemble_locked(self, transfer_id: str) -> tuple[str, dict[str, Any]]:
        meta = self._load_meta_locked(transfer_id)
        count = int(meta.get("chunk_count") or 0)
        missing = self._missing_locked(transfer_id, count)
        if missing:
            raise InboundTransferError(
                "INBOUND_TRANSFER_INCOMPLETE",
                data={
                    "transfer_id": transfer_id,
                    "missing_count": len(missing),
                    "missing_indexes": missing[:128],
                    "chunk_count": count,
                },
            )

        directory = self._dir(transfer_id)
        compressed = bytearray()
        try:
            for index in range(count):
                compressed.extend((directory / f"{index:04d}.part").read_bytes())
        except OSError as exc:
            raise InboundTransferError("INBOUND_CHUNK_READ_FAILED") from exc
        expected_compressed = int(meta.get("compressed_bytes") or 0)
        if len(compressed) != expected_compressed:
            raise InboundTransferError(
                "INBOUND_COMPRESSED_LENGTH_MISMATCH",
                data={"expected": expected_compressed, "actual": len(compressed)},
            )
        if len(compressed) > self.MAX_COMPRESSED_BYTES:
            raise InboundTransferError("INBOUND_COMPRESSED_SIZE_LIMIT")

        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(bytes(compressed), self.MAX_RAW_BYTES + 1)
            if (
                len(raw) > self.MAX_RAW_BYTES
                or decoder.unconsumed_tail
                or not decoder.eof
                or decoder.unused_data
            ):
                raise InboundTransferError("INBOUND_DECOMPRESSION_LIMIT_OR_TRAILING_DATA")
        except zlib.error as exc:
            raise InboundTransferError("INBOUND_DECOMPRESSION_FAILED") from exc

        expected_raw = int(meta.get("raw_bytes") or 0)
        if len(raw) != expected_raw:
            raise InboundTransferError(
                "INBOUND_RAW_LENGTH_MISMATCH",
                data={"expected": expected_raw, "actual": len(raw)},
            )
        actual_sha = hashlib.sha256(raw).hexdigest()
        expected_sha = str(meta.get("sha256") or "").casefold()
        if actual_sha != expected_sha:
            raise InboundTransferError(
                "INBOUND_SHA256_MISMATCH",
                data={"expected_sha256": expected_sha, "actual_sha256": actual_sha},
            )
        try:
            command = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InboundTransferError("INBOUND_INVALID_UTF8") from exc
        return command, {
            "transfer_id": transfer_id,
            "codec": "zlib",
            "raw_bytes": len(raw),
            "compressed_bytes": len(compressed),
            "chunk_count": count,
            "sha256": actual_sha,
            "verified": True,
        }

    def assemble(self, transfer_id: str) -> tuple[str, dict[str, Any]]:
        transfer_id = self._transfer_id(transfer_id)
        with self._lock:
            command, info = self._assemble_locked(transfer_id)
            os.utime(self._dir(transfer_id), None)
            return command, info

    def status(self, transfer_id: str) -> dict[str, Any]:
        transfer_id = self._transfer_id(transfer_id)
        with self._lock:
            meta = self._load_meta_locked(transfer_id)
            count = int(meta.get("chunk_count") or 0)
            missing = self._missing_locked(transfer_id, count)
            if missing:
                os.utime(self._dir(transfer_id), None)
                return {
                    "transfer_id": transfer_id,
                    "ready": False,
                    "verified": False,
                    "chunk_count": count,
                    "received_count": count - len(missing),
                    "missing_count": len(missing),
                    "missing_indexes": missing[:128],
                    "raw_bytes": int(meta.get("raw_bytes") or 0),
                    "compressed_bytes": int(meta.get("compressed_bytes") or 0),
                    "sha256": meta.get("sha256"),
                }
            _, info = self._assemble_locked(transfer_id)
            os.utime(self._dir(transfer_id), None)
            return {
                **info,
                "ready": True,
                "received_count": count,
                "missing_count": 0,
                "missing_indexes": [],
            }

    def abort(self, transfer_id: str) -> dict[str, Any]:
        transfer_id = self._transfer_id(transfer_id)
        with self._lock:
            directory = self._dir(transfer_id)
            existed = directory.exists()
            shutil.rmtree(directory, ignore_errors=True)
            return {"transfer_id": transfer_id, "removed": bool(existed)}
