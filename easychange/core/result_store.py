from __future__ import annotations

from pathlib import Path
from typing import Any
import base64
import binascii
import hashlib
import json
import threading
import time
import zlib


class DurableResultStore:
    """Bounded durable EC1 result/receipt store keyed by sequence."""

    def __init__(self, root: Path, *, max_results: int = 128, ttl_seconds: float = 1800.0) -> None:
        self.root = root
        self.max_results = max(16, int(max_results))
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sequence: str) -> Path:
        safe = "".join(ch for ch in sequence.upper() if ch.isalnum() or ch in "_-")
        if not safe or safe != sequence.upper():
            raise ValueError("INVALID_RESULT_SEQUENCE")
        return self.root / f"{safe}.json.zlib"

    @staticmethod
    def _encode(value: dict[str, Any]) -> bytes:
        return zlib.compress(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), level=6)

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any]:
        return json.loads(zlib.decompress(raw).decode("utf-8"))

    def _write(self, sequence: str, value: dict[str, Any]) -> None:
        target = self._path(sequence)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(self._encode(value))
        temporary.replace(target)
        self._cleanup()

    def _cleanup(self) -> None:
        now = time.time()
        files = sorted(self.root.glob("Q*.json.zlib"), key=lambda path: path.stat().st_mtime, reverse=True)
        for position, path in enumerate(files):
            try:
                age = now - path.stat().st_mtime
                if position >= self.max_results or age > self.ttl_seconds:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def get(self, sequence: str) -> dict[str, Any] | None:
        with self._lock:
            target = self._path(sequence)
            if not target.exists():
                return None
            try:
                value = self._decode(target.read_bytes())
            except (OSError, ValueError, json.JSONDecodeError, zlib.error):
                return None
            finished = float(value.get("finished_at") or value.get("created_at") or 0.0)
            if finished and time.time() - finished > self.ttl_seconds:
                target.unlink(missing_ok=True)
                return None
            return value

    def mark_running(self, sequence: str, command_hash: str, command: str, *, mutation: bool) -> dict[str, Any]:
        with self._lock:
            previous = self.get(sequence)
            if previous is not None:
                return previous
            value = {
                "sequence": sequence,
                "command_hash": command_hash,
                "command": command[:512],
                "mutation": bool(mutation),
                "state": "RUNNING",
                "created_at": time.time(),
                "started_at": time.time(),
                "finished_at": None,
                "result_hash": None,
                "result": None,
                "mutation_receipt": None,
            }
            self._write(sequence, value)
            return value

    def complete(
        self,
        sequence: str,
        command_hash: str,
        command: str,
        result: dict[str, Any],
        *,
        mutation_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._lock:
            current = self.get(sequence) or {}
            value = {
                **current,
                "sequence": sequence,
                "command_hash": command_hash,
                "command": command[:512],
                "mutation": bool(mutation_receipt),
                "state": "DONE",
                "created_at": current.get("created_at") or time.time(),
                "started_at": current.get("started_at") or time.time(),
                "finished_at": time.time(),
                "result_hash": hashlib.sha256(serialized).hexdigest(),
                "result_bytes": len(serialized),
                "result": result,
                "mutation_receipt": mutation_receipt,
            }
            self._write(sequence, value)
            return value

    def status(self, sequence: str) -> dict[str, Any] | None:
        value = self.get(sequence)
        if value is None:
            return None
        started = float(value.get("started_at") or 0.0)
        finished = float(value.get("finished_at") or 0.0)
        elapsed_end = finished or time.time()
        return {
            "sequence": sequence,
            "state": value.get("state"),
            "mutation": bool(value.get("mutation")),
            "command_hash": value.get("command_hash"),
            "result_hash": value.get("result_hash"),
            "result_bytes": value.get("result_bytes"),
            "started_at": value.get("started_at"),
            "finished_at": value.get("finished_at"),
            "duration_ms": int(max(0.0, elapsed_end - started) * 1000) if started else None,
            "mutation_receipt": value.get("mutation_receipt"),
        }

    def result(self, sequence: str) -> dict[str, Any] | None:
        value = self.get(sequence)
        result = value.get("result") if value else None
        return result if isinstance(result, dict) else None

    def _result_payload(self, sequence: str) -> tuple[dict[str, Any], bytes] | None:
        result = self.result(sequence)
        if result is None:
            return None
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return result, raw
    def chunk_meta(self, sequence: str, *, chunk_bytes: int = 640) -> dict[str, Any] | None:
        payload = self._result_payload(sequence)
        if payload is None:
            return None
        _, raw = payload
        size = max(256, min(4096, int(chunk_bytes)))
        total = max(1, (len(raw) + size - 1) // size)
        return {"sequence": sequence, "total_chunks": total, "chunk_bytes": size,
                "result_bytes": len(raw), "result_hash": hashlib.sha256(raw).hexdigest()}

    def chunk(self, sequence: str, index: int, *, chunk_bytes: int = 640) -> dict[str, Any] | None:
        payload = self._result_payload(sequence)
        if payload is None:
            return None
        _, raw = payload
        size = max(256, min(4096, int(chunk_bytes)))
        total = max(1, (len(raw) + size - 1) // size)
        if index < 0 or index >= total:
            raise IndexError("RESULT_CHUNK_OUT_OF_RANGE")
        chunk_payload = raw[index * size:(index + 1) * size]
        return {
            "sequence": sequence, "total_chunks": total, "chunk_bytes": size,
            "result_bytes": len(raw), "result_hash": hashlib.sha256(raw).hexdigest(),
            "chunk_index": index, "payload_bytes": len(chunk_payload),
            "crc32": f"{binascii.crc32(chunk_payload) & 0xFFFFFFFF:08x}",
            "payload_b64": base64.urlsafe_b64encode(chunk_payload).decode("ascii").rstrip("="),
            "has_more": index + 1 < total,
            "next_chunk_index": index + 1 if index + 1 < total else None,
            "previous_chunk_index": index - 1 if index > 0 else None,
        }
