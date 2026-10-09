from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any
import base64
import binascii
from contextlib import contextmanager
import hashlib
import json
import math
import os
import sys
import tempfile
import threading
import time
import zlib


MAX_COMPRESSED_RECEIPT_BYTES = 32 * 1024 * 1024


def _find_execution_summary(value: Any, depth: int = 0) -> dict[str, Any] | None:
    if depth > 6:
        return None
    if isinstance(value, dict):
        summary = value.get("test_summary")
        if isinstance(summary, dict):
            return dict(summary)
        marker_keys = {
            "finished", "return_code", "tests_passed", "tests_failed",
            "tests_skipped", "build_errors_count", "test_failures_count",
        }
        if "finished" in value and ("return_code" in value or "passed" in value):
            return {key: value.get(key) for key in marker_keys | {"passed"} if key in value}
        for key in ("data", "primary", "results", "test", "result"):
            if key in value:
                found = _find_execution_summary(value.get(key), depth + 1)
                if found:
                    return found
    elif isinstance(value, list):
        for item in value[:32]:
            found = _find_execution_summary(item, depth + 1)
            if found:
                return found
    return None


class DurableResultStore:
    """Bounded durable EC1 result/receipt store keyed by sequence."""

    def __init__(
        self,
        root: Path,
        *,
        max_results: int = 128,
        ttl_seconds: float = 1800.0,
        active_ttl_seconds: float = 24 * 60 * 60,
    ) -> None:
        self.root = root
        self.max_results = max(16, int(max_results))
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        # RUNNING/APPLIED-but-not-final records are execution truth. Pruning
        # them on the ordinary 30-minute result TTL can make a long build/test
        # or mutation look never-executed after restart and permit a duplicate.
        self.active_ttl_seconds = max(
            self.ttl_seconds,
            60 * 60,
            float(active_ttl_seconds),
        )
        self._lock = threading.RLock()
        # A separate OS lock protects the read/claim/write transaction when
        # multiple EasyChange processes share the same receipt directory.
        self._claim_lock_path = self.root / ".receipt-claim.lock"
        self._writes_since_cleanup = 0
        self._last_cleanup_monotonic = time.monotonic()
        self._payload_cache: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
        self._payload_cache_bytes = 0
        self._payload_cache_max_entries = 32
        self._payload_cache_max_bytes = 16 * 1024 * 1024
        self._terminal_header_cache: OrderedDict[
            str, tuple[dict[str, Any], tuple[int, int, int, int]]
        ] = OrderedDict()
        self._terminal_header_cache_max = 128
        self._stream_cache: OrderedDict[str, tuple[float, bytes, str, str, int]] = OrderedDict()
        self._stream_cache_bytes = 0
        self._stream_cache_max_entries = 16
        self._stream_cache_max_bytes = 16 * 1024 * 1024
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _claim_lock(self):
        # File locks are advisory: all claim writers must use this context.
        # A single byte is locked for the full read-then-write transaction.
        with self._lock:
            with open(self._claim_lock_path, "a+b") as handle:
                handle.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    # Initialize the lock byte only after acquiring the lock:
                    # concurrent openers must not race while growing the file.
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(bytes([0]))
                        handle.flush()
                    yield
                finally:
                    if sys.platform == "win32":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _path(self, sequence: str) -> Path:
        safe = "".join(ch for ch in sequence.upper() if ch.isalnum() or ch in "_-")
        if not safe or safe != sequence.upper():
            raise ValueError("INVALID_RESULT_SEQUENCE")
        return self.root / f"{safe}.json.zlib"

    @staticmethod
    def _encode(value: dict[str, Any]) -> bytes:
        # Durable results favor write/read latency over a few percent of disk
        # density. Local benchmarks on ~1.25 MiB structured results show level 1
        # is roughly 2x faster than level 6 while remaining compact enough for
        # the bounded 128-result store.
        return zlib.compress(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            level=1,
        )

    @staticmethod
    def _read_compressed(path: Path) -> bytes:
        # read_bytes() allocates the complete file *before* _decode() can
        # reject a corrupt oversized receipt. Bound disk reads at source.
        with path.open("rb") as stream:
            raw = stream.read(MAX_COMPRESSED_RECEIPT_BYTES + 1)
        if len(raw) > MAX_COMPRESSED_RECEIPT_BYTES:
            raise ValueError("RESULT_RECEIPT_COMPRESSED_TOO_LARGE")
        return raw

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any]:
        # Reject oversized compressed records and decompression bombs before
        # allocating an unbounded JSON document.
        max_compressed = MAX_COMPRESSED_RECEIPT_BYTES
        max_decoded = 64 * 1024 * 1024
        if len(raw) > max_compressed:
            raise ValueError("RESULT_RECEIPT_COMPRESSED_TOO_LARGE")
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(raw, max_decoded + 1)
        if len(decoded) > max_decoded or decoder.unconsumed_tail:
            raise ValueError("RESULT_RECEIPT_DECOMPRESSED_TOO_LARGE")
        decoded += decoder.flush(max_decoded + 1 - len(decoded))
        if len(decoded) > max_decoded or not decoder.eof or decoder.unused_data:
            raise ValueError("RESULT_RECEIPT_INVALID_STREAM")
        value = json.loads(decoded.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("RESULT_RECEIPT_NOT_OBJECT")
        return value


    def _payload_cache_put(self, sequence: str, raw: bytes, digest: str) -> None:
        size = len(raw)
        if size <= 0 or size > self._payload_cache_max_bytes:
            return
        previous = self._payload_cache.pop(sequence, None)
        if previous is not None:
            self._payload_cache_bytes -= len(previous[1])
        while (
            self._payload_cache
            and (
                len(self._payload_cache) >= self._payload_cache_max_entries
                or self._payload_cache_bytes + size > self._payload_cache_max_bytes
            )
        ):
            _, stale = self._payload_cache.popitem(last=False)
            self._payload_cache_bytes -= len(stale[1])
        self._payload_cache[sequence] = (time.monotonic(), raw, digest)
        self._payload_cache_bytes += size

    def _payload_cache_get(self, sequence: str) -> tuple[bytes, str] | None:
        cached = self._payload_cache.get(sequence)
        if cached is None:
            return None
        created, raw, digest = cached
        if time.monotonic() - created > self.ttl_seconds or not self._path(sequence).exists():
            self._payload_cache.pop(sequence, None)
            self._payload_cache_bytes -= len(raw)
            return None
        self._payload_cache.move_to_end(sequence)
        return raw, digest

    @staticmethod
    def _terminal_header_base(value: dict[str, Any]) -> dict[str, Any] | None:
        if str(value.get("state") or "").upper() != "DONE":
            return None
        result = value.get("result")
        return {
            "sequence": str(value.get("sequence") or ""),
            "execution_id": value.get("execution_id"),
            "command_id": (
                value.get("command_id")
                or (result.get("command_id") if isinstance(result, dict) else None)
            ),
            "started_at": value.get("started_at"),
            "finished_at": value.get("finished_at"),
            "finished": True,
            "state": value.get("state"),
            "execution_state": value.get("execution_state"),
            "mutation_state": value.get("mutation_state"),
            "mutation": bool(value.get("mutation")),
            "success": value.get("success"),
            "return_code": value.get("return_code"),
            "payload_length": int(value.get("result_bytes") or 0),
            "sha256": value.get("result_hash"),
            "summary": value.get("summary"),
            "mutation_receipt": value.get("mutation_receipt"),
        }

    @staticmethod
    def _receipt_stamp(path: Path) -> tuple[int, int, int, int]:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def _terminal_header_cache_put(self, sequence: str, value: dict[str, Any]) -> None:
        base = self._terminal_header_base(value)
        if base is None:
            self._terminal_header_cache.pop(sequence, None)
            return
        try:
            stamp = self._receipt_stamp(self._path(sequence))
        except OSError:
            self._terminal_header_cache.pop(sequence, None)
            return
        if sequence in self._terminal_header_cache:
            self._terminal_header_cache.pop(sequence, None)
        while len(self._terminal_header_cache) >= self._terminal_header_cache_max:
            self._terminal_header_cache.popitem(last=False)
        # Completed receipts remain replay tombstones even past the nominal
        # TTL. Cache by file version; cross-process deletion/replacement still
        # invalidates the entry without repeatedly decoding aged receipts.
        self._terminal_header_cache[sequence] = (base, stamp)

    def _terminal_header_cache_get(self, sequence: str) -> dict[str, Any] | None:
        entry = self._terminal_header_cache.get(sequence)
        if entry is None:
            return None
        base, stamp = entry
        try:
            if self._receipt_stamp(self._path(sequence)) != stamp:
                self._terminal_header_cache.pop(sequence, None)
                return None
        except OSError:
            self._terminal_header_cache.pop(sequence, None)
            return None
        self._terminal_header_cache.move_to_end(sequence)
        return dict(base)
    def _write(self, sequence: str, value: dict[str, Any], *, run_cleanup: bool = True) -> None:
        target = self._path(sequence)
        # Unique temp files avoid writers clobbering each other's staging file.
        # fsync before replacement so a successful write has a durable payload.
        fd, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(self._encode(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

        if run_cleanup:
            self._writes_since_cleanup += 1
            now = time.monotonic()
            if (
                self._writes_since_cleanup >= 8
                or now - self._last_cleanup_monotonic >= 30.0
            ):
                self._cleanup()

    @staticmethod
    def _active_record(value: dict[str, Any]) -> bool:
        state = str(value.get("state") or "").upper()
        execution = str(value.get("execution_state") or "").upper()
        mutation = str(value.get("mutation_state") or "").upper()
        # A mutation without a confirmed terminal outcome must not become
        # executable again just because its result-delivery TTL elapsed.
        uncertain_mutation = bool(value.get("mutation")) and mutation in {
            "STARTED", "APPLIED", "FAILED", "NOT_STARTED", "",
        }
        return state == "RUNNING" or execution in {"STARTED", "RUNNING"} or uncertain_mutation

    def _cleanup(self) -> None:
        now = time.time()
        self._last_cleanup_monotonic = time.monotonic()
        self._writes_since_cleanup = 0
        # Order is irrelevant to TTL/receipt safety. A global mtime sort
        # stat()s every receipt and materializes the full directory on each
        # maintenance pass; iterate lazily instead.
        files = self.root.glob("Q*.json.zlib")

        # Recent retained results need no decode at all. Decode only expired or
        # overflow candidates so large-result cleanup is proportional to data
        # actually eligible for removal, not every stored payload.
        for path in files:
            try:
                stat = path.stat()
                age = now - stat.st_mtime
                if age <= self.ttl_seconds:
                    # Recent receipts must survive even when the count limit
                    # is exceeded; otherwise replay can run a recent command.
                    continue
                try:
                    value = self._decode(self._read_compressed(path))
                except (OSError, ValueError, json.JSONDecodeError, zlib.error):
                    # Preserve uncertain receipts; corruption must never turn
                    # an already-executed command into a new command.
                    continue
                if self._active_record(value) or value.get("mutation") is True:
                    # Keep uncertain work and completed mutations in full.
                    continue
                if value.get("result_expired") is True:
                    continue
                sequence = value.get("sequence")
                if (not isinstance(sequence, str) or not sequence or
                        self._path(sequence) != path or value.get("state") != "DONE"):
                    # Unknown or corrupt records are evidence, never absence.
                    continue
                # Keep the execution identity; drop only the expired large
                # non-mutating result payload, avoiding unbounded disk growth.
                tombstone = {
                    key: value.get(key)
                    for key in (
                        "sequence", "execution_id", "command_hash", "command",
                        "command_id", "created_at", "started_at", "finished_at",
                        "updated_at", "state", "execution_state", "mutation_state",
                        "mutation", "success", "return_code",
                    )
                }
                tombstone["result_expired"] = True
                tombstone["expired_result_hash"] = value.get("result_hash")
                tombstone["result_bytes"] = 0
                tombstone["result"] = None
                self._write(sequence, tombstone, run_cleanup=False)
                old_payload = self._payload_cache.pop(sequence, None)
                if old_payload is not None:
                    self._payload_cache_bytes -= len(old_payload[1])
                old_stream = self._stream_cache.pop(sequence, None)
                if old_stream is not None:
                    self._stream_cache_bytes -= len(old_stream[1])
                self._terminal_header_cache.pop(sequence, None)
            except OSError:
                continue

    def get(self, sequence: str) -> dict[str, Any] | None:
        with self._lock:
            target = self._path(sequence)
            if not target.exists():
                return None
            try:
                value = self._decode(self._read_compressed(target))
            except (OSError, ValueError, json.JSONDecodeError, zlib.error) as exc:
                # A present but unreadable receipt is not a never-seen command.
                # Fail closed instead of allowing mark_running to execute again.
                raise RuntimeError("RESULT_RECEIPT_UNREADABLE") from exc
            if not isinstance(value, dict):
                raise RuntimeError("RESULT_RECEIPT_UNREADABLE")
            active = self._active_record(value)
            try:
                timestamp = float(
                    value.get("updated_at")
                    or value.get("finished_at")
                    or value.get("started_at")
                    or value.get("created_at")
                    or 0.0
                )
            except (TypeError, ValueError, OverflowError) as exc:
                # Corrupt receipt metadata must not be treated as absence.
                raise RuntimeError("RESULT_RECEIPT_UNREADABLE") from exc
            if not math.isfinite(timestamp) or timestamp < 0:
                raise RuntimeError("RESULT_RECEIPT_UNREADABLE")
            ttl = self.active_ttl_seconds if active else self.ttl_seconds
            if timestamp and time.time() - timestamp > ttl:
                if active:
                    # An unfinished execution remains an uncertain receipt.
                    return value
                # Retain expired receipts as replay tombstones. Deleting here
                # can make a previously executed sequence appear never seen.
                return value
            return value

    def mark_running(self, sequence: str, command_hash: str, command: str, *, mutation: bool) -> dict[str, Any]:
        # Compatibility wrapper: callers that only need the record keep the
        # historical return shape. Execution owners must use claim_running().
        value, _claimed = self.claim_running(
            sequence, command_hash, command, mutation=mutation)
        return value

    def claim_running(self, sequence: str, command_hash: str, command: str,
                      *, mutation: bool) -> tuple[dict[str, Any], bool]:
        """Atomically reserve a sequence across EasyChange processes.

        Only a newly claimed receipt authorizes execution. An existing RUNNING
        or DONE receipt is returned with claimed=False, even when a racing
        process wrote it after the caller's last get().
        """
        with self._claim_lock:
            previous = self.get(sequence)
            if previous is not None:
                if previous.get("command_hash") != command_hash:
                    raise ValueError("RESULT_SEQUENCE_COMMAND_HASH_MISMATCH")
                return previous, False
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
                "retention_ttl_seconds": self.ttl_seconds,
                "active_retention_ttl_seconds": self.active_ttl_seconds,
            }
            self._write(sequence, value)
            return value, True

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
        with self._claim_lock:
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
            self._terminal_header_cache_put(sequence, value)
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
        with self._lock:
            cached = self._terminal_header_cache_get(sequence)
        if cached is not None:
            size = max(256, min(4096, int(chunk_bytes)))
            result_bytes = int(cached.get("payload_length") or 0)
            cached["chunk_count"] = (
                max(1, (result_bytes + size - 1) // size) if result_bytes else 0
            )
            return cached

        # On a cache miss, read and populate under the cross-process claim
        # lock so another writer cannot replace the receipt between get()
        # and recording its file signature.
        with self._claim_lock:
            value = self.get(sequence)
            if value is None:
                return None
            base = self._terminal_header_base(value)
            if base is not None:
                self._terminal_header_cache_put(sequence, value)
        result = value.get("result")
        result_bytes = int(value.get("result_bytes") or 0)
        size = max(256, min(4096, int(chunk_bytes)))
        chunk_count = max(1, (result_bytes + size - 1) // size) if result_bytes else 0
        if base is not None:
            base["chunk_count"] = chunk_count
            return base
        return {
            "sequence": sequence,
            "execution_id": value.get("execution_id"),
            "command_id": (
                value.get("command_id")
                or (result.get("command_id") if isinstance(result, dict) else None)
            ),
            "started_at": value.get("started_at"),
            "finished_at": value.get("finished_at"),
            "finished": False,
            "state": value.get("state"),
            "execution_state": value.get("execution_state"),
            "mutation_state": value.get("mutation_state"),
            "mutation": bool(value.get("mutation")),
            "success": value.get("success"),
            "return_code": value.get("return_code"),
            "payload_length": result_bytes,
            "chunk_count": chunk_count,
            "sha256": value.get("result_hash"),
            "summary": value.get("summary"),
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
        serialized_hash = hashlib.sha256(serialized).hexdigest()
        execution_summary = _find_execution_summary(result)
        with self._claim_lock:
            current = self.get(sequence) or {}
            if current and current.get("command_hash") != command_hash:
                raise ValueError("RESULT_SEQUENCE_COMMAND_HASH_MISMATCH")
            if current.get("state") == "DONE":
                # Duplicate delivery must never rewrite the terminal receipt,
                # even when a retry arrives with a different result payload.
                return current
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
                "return_code": (
                    execution_summary.get("return_code")
                    if isinstance(execution_summary, dict) and execution_summary.get("return_code") is not None
                    else result.get("code")
                ),
                "summary": execution_summary,
                "result_hash": serialized_hash,
                "result_bytes": len(serialized),
                "result": result,
                "mutation_receipt": mutation_receipt,
            }
            self._write(sequence, value)
            self._payload_cache_put(sequence, serialized, serialized_hash)
            self._terminal_header_cache_put(sequence, value)
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
            "result_expired": value.get("result_expired") is True,
            "expired_result_hash": value.get("expired_result_hash"),
            "started_at": value.get("started_at"),
            "finished_at": value.get("finished_at"),
            "duration_ms": int(max(0.0, elapsed_end - started) * 1000) if started else None,
            "mutation_receipt": value.get("mutation_receipt"),
        }

    def result(self, sequence: str) -> dict[str, Any] | None:
        with self._lock:
            cached = self._payload_cache_get(sequence)
        if cached is not None:
            raw, _digest = cached
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = None
            if isinstance(value, dict):
                return value
        record = self.get(sequence)
        result = record.get("result") if record else None
        return result if isinstance(result, dict) else None

    def _result_payload(self, sequence: str) -> tuple[dict[str, Any], bytes, str] | None:
        with self._lock:
            cached = self._payload_cache_get(sequence)
            if cached is not None:
                raw, digest = cached
                return {}, raw, digest
        result = self.result(sequence)
        if result is None:
            return None
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        with self._lock:
            self._payload_cache_put(sequence, raw, digest)
        return result, raw, digest

    def _stream_cache_put(
        self,
        sequence: str,
        stream: bytes,
        stream_hash: str,
        result_hash: str,
        result_bytes: int,
    ) -> None:
        size = len(stream)
        if size <= 0 or size > self._stream_cache_max_bytes:
            return
        previous = self._stream_cache.pop(sequence, None)
        if previous is not None:
            self._stream_cache_bytes -= len(previous[1])
        while (
            self._stream_cache
            and (
                len(self._stream_cache) >= self._stream_cache_max_entries
                or self._stream_cache_bytes + size > self._stream_cache_max_bytes
            )
        ):
            _, stale = self._stream_cache.popitem(last=False)
            self._stream_cache_bytes -= len(stale[1])
        self._stream_cache[sequence] = (
            time.monotonic(),
            stream,
            stream_hash,
            result_hash,
            int(result_bytes),
        )
        self._stream_cache_bytes += size

    def _stream_cache_get(
        self,
        sequence: str,
    ) -> tuple[bytes, str, str, int] | None:
        cached = self._stream_cache.get(sequence)
        if cached is None:
            return None
        created, stream, stream_hash, result_hash, result_bytes = cached
        if time.monotonic() - created > self.ttl_seconds or not self._path(sequence).exists():
            self._stream_cache.pop(sequence, None)
            self._stream_cache_bytes -= len(stream)
            return None
        self._stream_cache.move_to_end(sequence)
        return stream, stream_hash, result_hash, result_bytes

    def _result_stream_payload(
        self,
        sequence: str,
    ) -> tuple[bytes, str, str, int] | None:
        with self._lock:
            cached = self._stream_cache_get(sequence)
            if cached is not None:
                return cached
        payload = self._result_payload(sequence)
        if payload is None:
            return None
        _, raw, result_hash = payload
        stream = zlib.compress(raw, level=1)
        stream_hash = hashlib.sha256(stream).hexdigest()
        with self._lock:
            self._stream_cache_put(
                sequence,
                stream,
                stream_hash,
                result_hash,
                len(raw),
            )
        return stream, stream_hash, result_hash, len(raw)

    def stream_meta(
        self,
        sequence: str,
        *,
        chunk_bytes: int = 2048,
        max_ratio: float = 0.85,
    ) -> dict[str, Any] | None:
        payload = self._result_stream_payload(sequence)
        if payload is None:
            return None
        stream, stream_hash, result_hash, result_bytes = payload
        size = max(256, min(4096, int(chunk_bytes)))
        ratio_limit = max(0.10, min(1.0, float(max_ratio)))
        ratio = (len(stream) / result_bytes) if result_bytes else 1.0
        eligible = bool(result_bytes >= 4096 and ratio <= ratio_limit)
        total = max(1, (len(stream) + size - 1) // size) if eligible else 0
        return {
            "sequence": sequence,
            "eligible": eligible,
            "encoding": "zlib-json-v1",
            "chunk_bytes": size,
            "total_chunks": total,
            "stream_bytes": len(stream),
            "stream_hash": stream_hash,
            "result_bytes": result_bytes,
            "result_hash": result_hash,
            "compression_ratio": round(ratio, 6),
            "max_ratio": ratio_limit,
        }

    def stream_chunks(
        self,
        sequence: str,
        indexes: list[int],
        *,
        chunk_bytes: int = 2048,
        max_chunks: int = 8,
        max_ratio: float = 0.85,
    ) -> dict[str, Any] | None:
        meta = self.stream_meta(
            sequence,
            chunk_bytes=chunk_bytes,
            max_ratio=max_ratio,
        )
        if meta is None:
            return None
        if not meta["eligible"]:
            return {**meta, "requested_indexes": [], "chunks": [], "complete": False}
        if not isinstance(indexes, list) or not indexes:
            raise ValueError("RESULT_STREAM_CHUNK_INDEXES_REQUIRED")
        limit = max(1, min(16, int(max_chunks)))
        unique: list[int] = []
        for raw_index in indexes:
            if (type(raw_index) is not int and
                    not (isinstance(raw_index, str) and raw_index.strip().isdecimal())):
                raise ValueError("INVALID_RESULT_STREAM_CHUNK_INDEX")
            index = int(raw_index)
            if index not in unique:
                unique.append(index)
        if len(unique) > limit:
            raise ValueError(f"RESULT_STREAM_CHUNK_BATCH_LIMIT:{limit}")

        payload = self._result_stream_payload(sequence)
        if payload is None:
            return None
        stream, stream_hash, result_hash, result_bytes = payload
        size = int(meta["chunk_bytes"])
        total = int(meta["total_chunks"])
        chunks: list[dict[str, Any]] = []
        for index in unique:
            if index < 0 or index >= total:
                raise IndexError("RESULT_STREAM_CHUNK_OUT_OF_RANGE")
            piece = stream[index * size:(index + 1) * size]
            chunks.append({
                "sequence": sequence,
                "encoding": "zlib-json-v1",
                "total_chunks": total,
                "chunk_bytes": size,
                "stream_bytes": len(stream),
                "stream_hash": stream_hash,
                "result_bytes": result_bytes,
                "result_hash": result_hash,
                "chunk_index": index,
                "payload_bytes": len(piece),
                "crc32": f"{binascii.crc32(piece) & 0xFFFFFFFF:08x}",
                "payload_b64": base64.urlsafe_b64encode(piece).decode("ascii").rstrip("="),
            })
        return {
            **meta,
            "requested_indexes": unique,
            "returned_indexes": [item["chunk_index"] for item in chunks],
            "chunks": chunks,
            "complete": len(chunks) == len(unique),
        }

    def stream_chunk(
        self,
        sequence: str,
        index: int,
        *,
        chunk_bytes: int = 2048,
        max_ratio: float = 0.85,
    ) -> dict[str, Any] | None:
        batch = self.stream_chunks(
            sequence,
            [int(index)],
            chunk_bytes=chunk_bytes,
            max_chunks=1,
            max_ratio=max_ratio,
        )
        if batch is None:
            return None
        chunks = batch.get("chunks")
        return chunks[0] if isinstance(chunks, list) and chunks else {
            key: batch.get(key)
            for key in (
                "sequence", "eligible", "encoding", "chunk_bytes",
                "total_chunks", "stream_bytes", "stream_hash",
                "result_bytes", "result_hash", "compression_ratio", "max_ratio",
            )
        }
    def chunk_meta(self, sequence: str, *, chunk_bytes: int = 640) -> dict[str, Any] | None:
        payload = self._result_payload(sequence)
        if payload is None:
            return None
        _, raw, digest = payload
        size = max(256, min(4096, int(chunk_bytes)))
        total = max(1, (len(raw) + size - 1) // size)
        return {"sequence": sequence, "total_chunks": total, "chunk_bytes": size,
                "result_bytes": len(raw), "result_hash": digest}


    @staticmethod
    def _chunk_payload_wire(chunk_payload: bytes, *, compress_payload: bool) -> dict[str, Any]:
        raw_crc = f"{binascii.crc32(chunk_payload) & 0xFFFFFFFF:08x}"
        wire = chunk_payload
        encoding = "base64"
        if compress_payload and len(chunk_payload) >= 256:
            compressed = zlib.compress(chunk_payload, level=1)
            # Keep raw when compression does not offset the encoding metadata.
            if len(compressed) + 16 < len(chunk_payload):
                wire = compressed
                encoding = "zlib+base64"
        return {
            "payload_bytes": len(chunk_payload),
            "wire_bytes": len(wire),
            "payload_encoding": encoding,
            "crc32": raw_crc,
            "payload_b64": base64.urlsafe_b64encode(wire).decode("ascii").rstrip("="),
        }

    def chunks(
        self,
        sequence: str,
        indexes: list[int],
        *,
        chunk_bytes: int = 640,
        max_chunks: int = 8,
        compress_payload: bool = False,
    ) -> dict[str, Any] | None:
        """Return several logical result chunks from one decoded durable payload.

        This avoids re-reading/decompressing/serializing the complete stored
        result once per chunk during MCP recovery. The returned batch is bounded
        so its own EC2 response remains reasonably small.
        """
        if not isinstance(indexes, list) or not indexes:
            raise ValueError("RESULT_CHUNK_INDEXES_REQUIRED")
        limit = max(1, min(16, int(max_chunks)))
        unique: list[int] = []
        for raw_index in indexes:
            if (type(raw_index) is not int and
                    not (isinstance(raw_index, str) and raw_index.strip().isdecimal())):
                raise ValueError("INVALID_RESULT_CHUNK_INDEX")
            index = int(raw_index)
            if index not in unique:
                unique.append(index)
        if len(unique) > limit:
            raise ValueError(f"RESULT_CHUNK_BATCH_LIMIT:{limit}")

        payload = self._result_payload(sequence)
        if payload is None:
            return None
        _, raw, result_hash = payload
        size = max(256, min(4096, int(chunk_bytes)))
        total = max(1, (len(raw) + size - 1) // size)
        chunks: list[dict[str, Any]] = []
        for index in unique:
            if index < 0 or index >= total:
                raise IndexError("RESULT_CHUNK_OUT_OF_RANGE")
            chunk_payload = raw[index * size:(index + 1) * size]
            chunks.append({
                "sequence": sequence,
                "total_chunks": total,
                "chunk_bytes": size,
                "result_bytes": len(raw),
                "result_hash": result_hash,
                "chunk_index": index,
                **self._chunk_payload_wire(
                    chunk_payload,
                    compress_payload=bool(compress_payload),
                ),
                "has_more": index + 1 < total,
                "next_chunk_index": index + 1 if index + 1 < total else None,
                "previous_chunk_index": index - 1 if index > 0 else None,
            })
        return {
            "sequence": sequence,
            "requested_indexes": unique,
            "returned_indexes": [item["chunk_index"] for item in chunks],
            "total_chunks": total,
            "chunk_bytes": size,
            "result_bytes": len(raw),
            "result_hash": result_hash,
            "compressed_requested": bool(compress_payload),
            "compressed_chunks": sum(
                1 for item in chunks if item.get("payload_encoding") == "zlib+base64"
            ),
            "chunks": chunks,
            "complete": len(chunks) == len(unique),
        }

    def chunk(
        self,
        sequence: str,
        index: int,
        *,
        chunk_bytes: int = 640,
        compress_payload: bool = False,
    ) -> dict[str, Any] | None:
        payload = self._result_payload(sequence)
        if payload is None:
            return None
        _, raw, digest = payload
        size = max(256, min(4096, int(chunk_bytes)))
        total = max(1, (len(raw) + size - 1) // size)
        if index < 0 or index >= total:
            raise IndexError("RESULT_CHUNK_OUT_OF_RANGE")
        chunk_payload = raw[index * size:(index + 1) * size]
        return {
            "sequence": sequence, "total_chunks": total, "chunk_bytes": size,
            "result_bytes": len(raw), "result_hash": digest,
            "chunk_index": index,
            **self._chunk_payload_wire(
                chunk_payload,
                compress_payload=bool(compress_payload),
            ),
            "has_more": index + 1 < total,
            "next_chunk_index": index + 1 if index + 1 < total else None,
            "previous_chunk_index": index - 1 if index > 0 else None,
        }
