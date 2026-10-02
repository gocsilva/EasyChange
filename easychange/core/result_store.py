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
            now = time.time()
            execution_id = "E" + hashlib.sha256(
                f"{sequence}:{command_hash}:{time.time_ns()}".encode("utf-8")
            ).hexdigest()[:16].upper()
            value = {
                "sequence": sequence,
                "execution_id": execution_id,
                "command_hash": command_hash,
                "command": command[:512],
                "command_id": None,
                "mutation": bool(mutation),
                "state": "RUNNING",
                "execution_state": "STARTED",
                "mutation_state": "STARTED" if mutation else "NOT_APPLICABLE",
                "created_at": now,
                "started_at": now,
                "finished_at": None,
                "success": None,
                "return_code": None,
                "result_hash": None,
                "result_bytes": None,
                "result": None,
                "mutation_receipt": None,
            }
            self._write(sequence, value)
            return value

    def update_mutation_state(
        self,
        sequence: str,
        lifecycle_state: str,
        *,
        mutation_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        allowed = {"NOT_STARTED", "STARTED", "APPLIED", "VALIDATED", "ROLLED_BACK", "FAILED"}
        state = str(lifecycle_state or "").upper()
        if state not in allowed:
            raise ValueError(f"INVALID_MUTATION_STATE:{state}")
        with self._lock:
            current = self.get(sequence)
            if current is None:
                return None
            value = {
                **current,
                "mutation_state": state,
                "mutation_receipt": (
                    mutation_receipt
                    if mutation_receipt is not None
                    else current.get("mutation_receipt")
                ),
                "updated_at": time.time(),
            }
            self._write(sequence, value)
            return value

    def update_mutation_receipt(
        self,
        sequence: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not isinstance(receipt, dict):
            raise TypeError("MUTATION_RECEIPT_OBJECT_REQUIRED")
        lifecycle = str(
            receipt.get("lifecycle_state")
            or receipt.get("state")
            or "STARTED"
        ).upper()
        legacy_map = {
            "COMMITTED": "VALIDATED",
            "ROLLBACK_UNKNOWN": "FAILED",
            "ACTIVE": "STARTED",
        }
        lifecycle = legacy_map.get(lifecycle, lifecycle)
        return self.update_mutation_state(
            sequence,
            lifecycle,
            mutation_receipt=dict(receipt),
        )

    def header(self, sequence: str, *, chunk_bytes: int = 640) -> dict[str, Any] | None:
        value = self.get(sequence)
        if value is None:
            return None
        result = value.get("result")
        result_bytes = int(value.get("result_bytes") or 0)
        size = max(256, min(4096, int(chunk_bytes)))
        chunk_count = max(1, (result_bytes + size - 1) // size) if result_bytes else 0
        return {
            "sequence": sequence,
            "execution_id": value.get("execution_id"),
            "command_id": (
                value.get("command_id")
                or (result.get("command_id") if isinstance(result, dict) else None)
            ),
            "started_at": value.get("started_at"),
            "finished_at": value.get("finished_at"),
            "finished": value.get("state") == "DONE",
            "state": value.get("state"),
            "execution_state": value.get("execution_state"),
            "mutation_state": value.get("mutation_state"),
            "mutation": bool(value.get("mutation")),
            "success": value.get("success"),
            "return_code": value.get("return_code"),
            "payload_length": result_bytes,
            "chunk_count": chunk_count,
            "sha256": value.get("result_hash"),
            "mutation_receipt": value.get("mutation_receipt"),
        }

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
            success = bool(result.get("ok"))
            lifecycle = None
            if isinstance(mutation_receipt, dict):
                lifecycle = str(
                    mutation_receipt.get("lifecycle_state")
                    or mutation_receipt.get("state")
                    or ""
                ).upper() or None
            if not lifecycle and current.get("mutation"):
                lifecycle = str(current.get("mutation_state") or "FAILED").upper()
            value = {
                **current,
                "sequence": sequence,
                "execution_id": current.get("execution_id") or (
                    "E" + hashlib.sha256(
                        f"{sequence}:{command_hash}:{time.time_ns()}".encode("utf-8")
                    ).hexdigest()[:16].upper()
                ),
                "command_hash": command_hash,
                "command": command[:512],
                "command_id": result.get("command_id"),
                "mutation": bool(current.get("mutation") or mutation_receipt),
                "state": "DONE",
                "execution_state": "FINISHED",
                "mutation_state": lifecycle or (
                    "NOT_APPLICABLE" if not current.get("mutation") else "FAILED"
                ),
                "created_at": current.get("created_at") or time.time(),
                "started_at": current.get("started_at") or time.time(),
                "finished_at": time.time(),
                "success": success,
                "return_code": result.get("code"),
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
            "execution_id": value.get("execution_id"),
            "command_id": value.get("command_id"),
            "state": value.get("state"),
            "execution_state": value.get("execution_state"),
            "mutation_state": value.get("mutation_state"),
            "mutation": bool(value.get("mutation")),
            "success": value.get("success"),
            "return_code": value.get("return_code"),
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
