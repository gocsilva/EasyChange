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
import stat
import sys
import tempfile
import threading
import time
import zlib


MAX_COMPRESSED_RECEIPT_BYTES = 32 * 1024 * 1024
MAX_DECODED_RECEIPT_BYTES = 64 * 1024 * 1024
MAX_CACHED_TERMINAL_HEADER_BYTES = 32 * 1024  # 128 entries => at most 4 MiB
MAX_COMPRESSED_RESULT_STREAM_BYTES = 16 * 1024 * 1024


def _receipt_pairs_unique(pairs):
    value = {}
    for key, member in pairs:
        if key in value:
            raise ValueError("RESULT_RECEIPT_DUPLICATE_KEY")
        value[key] = member
    return value


def _receipt_nonfinite_constant(_value):
    raise ValueError("RESULT_RECEIPT_NONFINITE_NUMBER")


def _receipt_finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("RESULT_RECEIPT_NONFINITE_NUMBER")
    return number


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
        normal_ttl = float(ttl_seconds)
        active_ttl = float(active_ttl_seconds)
        # Nonfinite retention can disable cleanup or make expiry ambiguous.
        if not math.isfinite(normal_ttl) or not math.isfinite(active_ttl):
            raise ValueError("INVALID_RESULT_RETENTION_TTL")
        self.ttl_seconds = max(60.0, normal_ttl)
        # RUNNING/APPLIED-but-not-final records are execution truth. Pruning
        # them on the ordinary 30-minute result TTL can make a long build/test
        # or mutation look never-executed after restart and permit a duplicate.
        self.active_ttl_seconds = max(
            self.ttl_seconds,
            60 * 60,
            active_ttl,
        )
        self._lock = threading.RLock()
        # A separate OS lock protects the read/claim/write transaction when
        # multiple EasyChange processes share the same receipt directory.
        self._claim_lock_path = self.root / ".receipt-claim.lock"
        self._writes_since_cleanup = 0
        self._last_cleanup_monotonic = time.monotonic()
        self._payload_cache: OrderedDict[str, tuple[float, bytes, str, tuple[int, int, int, int]]] = OrderedDict()
        self._payload_cache_bytes = 0
        self._payload_cache_max_entries = 32
        self._payload_cache_max_bytes = 16 * 1024 * 1024
        # Cache an immutable, bounded snapshot instead of borrowed nested
        # dictionaries from the receipt; callers cannot mutate cached truth.
        self._terminal_header_cache: OrderedDict[
            str, tuple[bytes, tuple[int, int, int, int]]
        ] = OrderedDict()
        self._terminal_header_cache_max = 128
        self._stream_cache: OrderedDict[str, tuple[float, bytes, str, str, int, tuple[int, int, int, int]]] = OrderedDict()
        self._stream_cache_bytes = 0
        self._stream_cache_max_entries = 16
        self._stream_cache_max_bytes = 16 * 1024 * 1024
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _claim_lock(self):
        # File locks are advisory: all claim writers must use this context.
        # A single byte is locked for the full read-then-write transaction.
        with self._lock:
            # The claim inode is the authority for cross-process mutation
            # ownership. Never follow a symlink to an unrelated file/lock.
            if self._claim_lock_path.is_symlink():
                raise RuntimeError("RESULT_CLAIM_UNSAFE_LOCK_PATH")
            flags=(os.O_CREAT|os.O_RDWR|getattr(os,"O_BINARY",0)|
                   getattr(os,"O_NOFOLLOW",0))
            try:
                fd=os.open(self._claim_lock_path,flags,0o600)
            except OSError as exc:
                raise RuntimeError("RESULT_CLAIM_LOCK_UNAVAILABLE") from exc
            try:
                handle=os.fdopen(fd,"r+b")
            except BaseException:
                os.close(fd)
                raise
            with handle:
                metadata=os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise RuntimeError("RESULT_CLAIM_UNSAFE_LOCK_TYPE")
                if metadata.st_nlink != 1:
                    # Even a private, regular lock file must have a unique
                    # name: hard-link aliases break the ownership boundary.
                    # Check Windows too when the filesystem reports links.
                    raise RuntimeError("RESULT_CLAIM_UNSAFE_LOCK_LINKS")
                if os.name=="posix" and (
                        metadata.st_uid!=os.getuid() or
                        metadata.st_mode & 0o022):
                    # The predictable lock file must not be writable by a
                    # different user. Otherwise a second inode or tampered
                    # lock can invalidate cross-process sequence ownership.
                    raise RuntimeError("RESULT_CLAIM_UNSAFE_LOCK_OWNER")
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
        # Unicode uppercasing is not injective (e.g. 'Qß' and 'QSS' both
        # become 'QSS'), which could alias two distinct durable executions
        # to one receipt. Validate raw ASCII before normalizing the filename.
        if (not isinstance(sequence,str) or not 1<=len(sequence)<=128
                or not sequence.isascii()):
            raise ValueError("INVALID_RESULT_SEQUENCE")
        safe=sequence.upper()
        if any(not (ch.isalnum() or ch in "_-") for ch in safe):
            raise ValueError("INVALID_RESULT_SEQUENCE")
        return self.root / f"{safe}.json.zlib"

    @staticmethod
    def _encode(value: dict[str, Any]) -> bytes:
        # Encode directly into zlib instead of retaining the entire JSON
        # alongside its compressed copy; preserve level 1 and the wire schema.
        compressor = zlib.compressobj(level=1)
        encoded = bytearray()
        decoded_size = 0
        encoder = json.JSONEncoder(
            ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        for fragment in encoder.iterencode(value):
            if len(fragment) > MAX_DECODED_RECEIPT_BYTES:
                raise ValueError("RESULT_RECEIPT_DECOMPRESSED_TOO_LARGE")
            for offset in range(0, len(fragment), 4096):
                piece = fragment[offset:offset+4096].encode("utf-8")
                decoded_size += len(piece)
                if decoded_size > MAX_DECODED_RECEIPT_BYTES:
                    raise ValueError("RESULT_RECEIPT_DECOMPRESSED_TOO_LARGE")
                block = compressor.compress(piece)
                if len(encoded) + len(block) > MAX_COMPRESSED_RECEIPT_BYTES:
                    raise ValueError("RESULT_RECEIPT_COMPRESSED_TOO_LARGE")
                encoded.extend(block)
        tail = compressor.flush()
        if len(encoded) + len(tail) > MAX_COMPRESSED_RECEIPT_BYTES:
            raise ValueError("RESULT_RECEIPT_COMPRESSED_TOO_LARGE")
        encoded.extend(tail)
        return bytes(encoded)

    @staticmethod
    def _read_compressed(path: Path) -> bytes:
        # read_bytes() allocates the complete file *before* _decode() can
        # reject a corrupt oversized receipt. Bound disk reads at source.
        # Check the actual opened file, not only a pathname inspected
        # earlier by get(). On POSIX, O_NOFOLLOW prevents a symlink swap
        # between the initial identity check and this disk read.
        # A malicious or corrupt FIFO named like a receipt must not block
        # os.open() forever before the fstat() regular-file guard runs.
        # O_NONBLOCK is used for open only; regular files are unaffected.
        flags=(os.O_RDONLY|getattr(os,"O_BINARY",0)|
               getattr(os,"O_NOFOLLOW",0)|
               (getattr(os,"O_NONBLOCK",0) if os.name=="posix" else 0))
        fd=os.open(path,flags)
        try:
            stream=os.fdopen(fd,"rb")
        except BaseException:
            # If fdopen itself fails, no context manager owns the raw fd.
            # Release it rather than leaking a handle on every retry.
            os.close(fd)
            raise
        with stream:
            metadata=os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1:
                # A hard-linked receipt can alias a file outside the result
                # store despite a safe-looking on-disk pathname.
                raise ValueError("RESULT_RECEIPT_UNSAFE_TYPE")
            raw=stream.read(MAX_COMPRESSED_RECEIPT_BYTES+1)
        if len(raw)>MAX_COMPRESSED_RECEIPT_BYTES:
            raise ValueError("RESULT_RECEIPT_COMPRESSED_TOO_LARGE")
        return raw

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any]:
        # Reject oversized compressed records and decompression bombs before
        # allocating an unbounded JSON document.
        max_compressed = MAX_COMPRESSED_RECEIPT_BYTES
        max_decoded = MAX_DECODED_RECEIPT_BYTES
        if len(raw) > max_compressed:
            raise ValueError("RESULT_RECEIPT_COMPRESSED_TOO_LARGE")
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(raw, max_decoded + 1)
        if len(decoded) > max_decoded or decoder.unconsumed_tail:
            raise ValueError("RESULT_RECEIPT_DECOMPRESSED_TOO_LARGE")
        decoded += decoder.flush(max_decoded + 1 - len(decoded))
        if len(decoded) > max_decoded or not decoder.eof or decoder.unused_data:
            raise ValueError("RESULT_RECEIPT_INVALID_STREAM")
        value = json.loads(
            decoded.decode("utf-8"),
            object_pairs_hook=_receipt_pairs_unique,
            parse_constant=_receipt_nonfinite_constant,
            parse_float=_receipt_finite_float,
        )
        if not isinstance(value, dict):
            raise ValueError("RESULT_RECEIPT_NOT_OBJECT")
        return value


    def _payload_cache_put(self, sequence: str, raw: bytes, digest: str,
                           *, stamp: tuple[int, int, int, int] | None = None) -> None:
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
        if stamp is None:
            try:
                stamp = self._receipt_stamp(self._path(sequence))
            except OSError:
                return
        self._payload_cache[sequence] = (time.monotonic(), raw, digest, stamp)
        self._payload_cache_bytes += size

    def _payload_cache_get(self, sequence: str) -> tuple[bytes, str] | None:
        cached = self._payload_cache.get(sequence)
        if cached is None:
            return None
        created, raw, digest, stamp = cached
        try:
            unchanged = self._receipt_stamp(self._path(sequence)) == stamp
        except OSError:
            unchanged = False
        if time.monotonic() - created > self.ttl_seconds or not unchanged:
            self._payload_cache.pop(sequence, None)
            self._payload_cache_bytes -= len(raw)
            return None
        self._payload_cache.move_to_end(sequence)
        return raw, digest

    @staticmethod
    def _receipt_result_size(value: Any) -> int:
        """Interpret bounded legacy size metadata without untrusted int()."""
        if value is None:
            return 0
        if type(value) is int:
            amount = value
        elif (isinstance(value, str) and 1 <= len(value) <= 8
              and value.isascii() and value.isdecimal()):
            amount = int(value)
        else:
            raise RuntimeError("RESULT_RECEIPT_UNREADABLE")
        if not 0 <= amount <= MAX_DECODED_RECEIPT_BYTES:
            raise RuntimeError("RESULT_RECEIPT_UNREADABLE")
        return amount

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
            "payload_length": DurableResultStore._receipt_result_size(
                value.get("result_bytes")),
            "sha256": value.get("result_hash"),
            "result_expired": value.get("result_expired") is True,
            "expired_result_hash": value.get("expired_result_hash"),
            "summary": value.get("summary"),
            "mutation_receipt": value.get("mutation_receipt"),
        }

    @staticmethod
    def _receipt_stamp(path: Path) -> tuple[int, int, int, int]:
        # lstat checks the actual pathname, not its symlink destination.
        # A replaced cached receipt must never remain trusted merely because
        # a symbolic link now points back to the original inode.
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            # A safe receipt later gaining a hard-link alias keeps the same
            # inode and mtime. Invalidate cached headers on that transition.
            raise OSError("RESULT_RECEIPT_UNSAFE_TYPE")
        return (metadata.st_dev, metadata.st_ino,
                metadata.st_size, metadata.st_mtime_ns)

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
        self._terminal_header_cache.pop(sequence, None)
        # An entry cap alone does not bound memory when a mutation receipt
        # contains a large nested summary. Stop JSON encoding before copying
        # anything that would exceed the per-header byte budget.
        encoded = bytearray()
        try:
            for fragment in json.JSONEncoder(
                    ensure_ascii=False, allow_nan=False,
                    separators=(",", ":")).iterencode(base):
                if len(fragment) > MAX_CACHED_TERMINAL_HEADER_BYTES:
                    return
                wire = fragment.encode("utf-8")
                if len(encoded) + len(wire) > MAX_CACHED_TERMINAL_HEADER_BYTES:
                    return
                encoded.extend(wire)
        except (TypeError, ValueError, RecursionError, UnicodeError):
            # An unexpected header shape is never a reason to lose the
            # underlying receipt or fail its terminal write.
            return
        while len(self._terminal_header_cache) >= self._terminal_header_cache_max:
            self._terminal_header_cache.popitem(last=False)
        # File version invalidates cross-process replacement; immutable wire
        # prevents nested mutable references leaking into the cache.
        self._terminal_header_cache[sequence] = (bytes(encoded), stamp)

    def _terminal_header_cache_get(self, sequence: str) -> dict[str, Any] | None:
        entry = self._terminal_header_cache.get(sequence)
        if entry is None:
            return None
        snapshot, stamp = entry
        try:
            if self._receipt_stamp(self._path(sequence)) != stamp:
                self._terminal_header_cache.pop(sequence, None)
                return None
        except OSError:
            self._terminal_header_cache.pop(sequence, None)
            return None
        self._terminal_header_cache.move_to_end(sequence)
        return json.loads(snapshot)
    def _write(self, sequence: str, value: dict[str, Any], *, run_cleanup: bool = True) -> None:
        target = self._path(sequence)
        # Unique temp files avoid writers clobbering each other's staging file.
        # fsync before replacement so a successful write has a durable payload.
        fd, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=self.root)
        temporary = Path(name)
        try:
            try:
                stream = os.fdopen(fd, "wb")
            except BaseException:
                # mkstemp() owns the descriptor until fdopen() succeeds.
                # A wrapper failure must not leak the writable staging fd.
                os.close(fd)
                raise
            with stream:
                stream.write(self._encode(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            if os.name == "posix":
                # The file fsync above persists its bytes; fsync the parent
                # directory after the atomic rename so the receipt name is
                # also durable across sudden power loss where supported.
                # Some filesystems do not permit directory fsync; the atomic
                # replace still stands and recovery remains fail-closed.
                directory_fd = None
                try:
                    directory_fd = os.open(
                        self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    os.fsync(directory_fd)
                except OSError:
                    pass
                finally:
                    if directory_fd is not None:
                        os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

        if run_cleanup:
            self._writes_since_cleanup += 1
            elapsed = time.monotonic() - self._last_cleanup_monotonic
            # Maintenance stat()s every stored receipt. At high write rates,
            # scanning the entire directory every eight writes turns O(n)
            # cleanup into a major bottleneck. Bound scan frequency by time;
            # TTL is at least 60s, so a 5s minimum never weakens retention.
            if (elapsed >= 30.0 or
                    (self._writes_since_cleanup >= 128 and elapsed >= 5.0)):
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
        # _path() also accepts legitimate non-Q ASCII sequence identifiers
        # (e.g. direct ResultStore callers). Scan all receipt-shaped names,
        # then retain the existing strict decoded-sequence/path match below.
        # Otherwise old non-Q results keep full payloads forever.
        files = self.root.glob("*.json.zlib")

        # Recent retained results need no decode at all. Decode only expired or
        # overflow candidates so large-result cleanup is proportional to data
        # actually eligible for removal, not every stored payload.
        for path in files:
            try:
                # Never follow a forged receipt symlink outside the store
                # during retention; unreadable receipts remain evidence.
                if path.is_symlink():
                    continue
                stat = path.stat()
                age = now - stat.st_mtime
                if age <= self.ttl_seconds:
                    # Recent receipts must survive even when the count limit
                    # is exceeded; otherwise replay can run a recent command.
                    continue
                try:
                    value = self._decode(self._read_compressed(path))
                except (OSError, ValueError, TypeError, OverflowError,
                        RecursionError, zlib.error):
                    # Preserve uncertain receipts; corruption must never turn
                    # an already-executed command into a new command.
                    continue
                # Legacy/imported mutation receipts can carry a mutation
                # lifecycle or receipt even when the old boolean flag is
                # absent. Never shrink these records into non-mutation
                # tombstones and discard rollback/verification evidence.
                mutation_state = value.get("mutation_state")
                if (self._active_record(value)
                        or value.get("mutation") is True
                        or value.get("mutation_receipt") is not None
                        or mutation_state not in (None, "", "NOT_APPLICABLE")):
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
            except (OSError, ValueError, TypeError, OverflowError,
                    RecursionError):
                # A decoded but malformed legacy receipt can contain a
                # sequence that _path() rejects. Preserve its evidence and
                # continue cleaning independent receipts.
                continue

    def get(self, sequence: str) -> dict[str, Any] | None:
        with self._lock:
            target = self._path(sequence)
            # A present symlink (including a dangling one) is not an absent
            # sequence. Reject it before replay/claim to prevent unrelated
            # filesystem content being interpreted as an execution receipt.
            if target.is_symlink():
                raise RuntimeError("RESULT_RECEIPT_UNREADABLE")
            if not target.exists():
                return None
            try:
                value = self._decode(self._read_compressed(target))
            except (OSError, ValueError, UnicodeError, OverflowError,
                    RecursionError, zlib.error) as exc:
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
        with self._claim_lock():
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
        with self._claim_lock():
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
        # Header polling is as frequent as chunk recovery. Reject a malformed
        # size before taking locks, hitting the disk or decoding a receipt.
        size = self._validated_chunk_size(chunk_bytes)
        with self._lock:
            cached = self._terminal_header_cache_get(sequence)
        if cached is not None:
            size = self._validated_chunk_size(chunk_bytes)
            result_bytes = self._receipt_result_size(cached.get("payload_length"))
            cached["chunk_count"] = (
                max(1, (result_bytes + size - 1) // size) if result_bytes else 0
            )
            return cached

        # On a cache miss, read and populate under the cross-process claim
        # lock so another writer cannot replace the receipt between get()
        # and recording its file signature.
        with self._claim_lock():
            value = self.get(sequence)
            if value is None:
                return None
            base = self._terminal_header_base(value)
            if base is not None:
                self._terminal_header_cache_put(sequence, value)
        result = value.get("result")
        result_bytes = self._receipt_result_size(value.get("result_bytes"))
        size = self._validated_chunk_size(chunk_bytes)
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
        with self._claim_lock():
            current = self.get(sequence)
            if current is None:
                # A caller must first win claim_running(). Otherwise a
                # misplaced completion could forge a terminal result without
                # ever reserving the sequence against concurrent execution.
                raise RuntimeError("RESULT_SEQUENCE_NOT_CLAIMED")
            if current.get("command_hash") != command_hash:
                raise ValueError("RESULT_SEQUENCE_COMMAND_HASH_MISMATCH")
            if current.get("state") == "DONE":
                # Duplicate delivery must never rewrite the terminal receipt,
                # even when a retry arrives with a different result payload.
                return current
        # No writer needs the receipt lock while a potentially large tool
        # result is serialized. Re-check under the lock afterward: another
        # process may have completed this sequence during serialization.
        # A caller can provide a huge result or a compact shared DAG whose
        # JSON expansion is enormous. Enforce the same receipt-size budget
        # *during* encoding, before constructing an unbounded json.dumps().
        serialized = self._encode_result_json(result)
        # Persist the exact JSON snapshot whose bytes are hashed. Another
        # worker can still mutate the caller's result while we re-acquire the
        # durable claim lock; using that live dict corrupts replay hashes.
        frozen_result = json.loads(serialized)
        serialized_hash = hashlib.sha256(serialized).hexdigest()
        execution_summary = _find_execution_summary(frozen_result)
        with self._claim_lock():
            current = self.get(sequence)
            if current is None:
                raise RuntimeError("RESULT_SEQUENCE_NOT_CLAIMED")
            if current.get("command_hash") != command_hash:
                raise ValueError("RESULT_SEQUENCE_COMMAND_HASH_MISMATCH")
            if current.get("state") == "DONE":
                return current
            success = frozen_result.get("ok") is True
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
                "command_id": frozen_result.get("command_id"),
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
                    else frozen_result.get("code")
                ),
                "summary": execution_summary,
                "result_hash": serialized_hash,
                "result_bytes": len(serialized),
                "result": frozen_result,
                # An earlier update_mutation_receipt() may have persisted the
                # verified physical/file side-effect before terminal delivery.
                # Omitting a new receipt must never erase that audit evidence.
                "mutation_receipt": (
                    mutation_receipt if mutation_receipt is not None
                    else current.get("mutation_receipt")
                ),
            }
            self._write(sequence, value)
            self._payload_cache_put(sequence, serialized, serialized_hash)
            self._terminal_header_cache_put(sequence, value)
            return value

    def status(self, sequence: str) -> dict[str, Any] | None:
        value = self.get(sequence)
        if value is None:
            return None
        try:
            started = float(value.get("started_at") or 0.0)
            finished = float(value.get("finished_at") or 0.0)
        except (TypeError, ValueError, OverflowError) as exc:
            # A valid updated_at can coexist with corrupt legacy timing fields.
            # Never manufacture duration_ms or mask the durable replay claim.
            raise RuntimeError("RESULT_RECEIPT_UNREADABLE") from exc
        if (not math.isfinite(started) or not math.isfinite(finished) or
                started < 0 or finished < 0):
            raise RuntimeError("RESULT_RECEIPT_UNREADABLE")
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

    @staticmethod
    def _encode_result_json(value: dict[str, Any]) -> bytes:
        """Encode recovered payload once, under the receipt's byte budget."""
        wire=bytearray()
        encoder=json.JSONEncoder(
            ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        for fragment in encoder.iterencode(value):
            if len(fragment)>MAX_DECODED_RECEIPT_BYTES:
                raise ValueError("RESULT_RECEIPT_DECOMPRESSED_TOO_LARGE")
            # JSONEncoder may yield a single large string. Never allocate a
            # full additional UTF-8 copy of that fragment just to reject it
            # against the byte budget: slice on code-point boundaries first.
            for offset in range(0,len(fragment),4096):
                encoded=fragment[offset:offset+4096].encode("utf-8")
                if len(wire)+len(encoded)>MAX_DECODED_RECEIPT_BYTES:
                    raise ValueError("RESULT_RECEIPT_DECOMPRESSED_TOO_LARGE")
                wire.extend(encoded)
        return bytes(wire)

    def _result_payload(self, sequence: str) -> tuple[dict[str, Any], bytes, str] | None:
        with self._lock:
            cached = self._payload_cache_get(sequence)
            if cached is not None:
                raw, digest = cached
                return {}, raw, digest
        path = self._path(sequence)
        for _ in range(3):
            try:
                before = self._receipt_stamp(path)
            except FileNotFoundError:
                return None
            result = self.result(sequence)
            if result is None:
                return None
            raw = self._encode_result_json(result)
            digest = hashlib.sha256(raw).hexdigest()
            try:
                after = self._receipt_stamp(path)
            except FileNotFoundError:
                continue
            if before != after:
                continue
            with self._lock:
                # Use the stamp of the bytes actually read. Re-stat'ing in
                # cache_put could associate an older payload with a newer
                # receipt written by another process in the meantime.
                self._payload_cache_put(sequence, raw, digest, stamp=after)
            return result, raw, digest
        raise RuntimeError("RESULT_RECEIPT_CHANGED_DURING_READ")

    def _stream_cache_put(
        self,
        sequence: str,
        stream: bytes,
        stream_hash: str,
        result_hash: str,
        result_bytes: int,
        *,
        stamp: tuple[int, int, int, int] | None = None,
    ) -> None:
        size = len(stream)
        # An empty stream is the bounded compressor's negative-cache entry.
        # Retain it for the normal cache TTL: repeated EC2H polls must not
        # recompress an incompressible 64 MiB receipt every time.
        if size > self._stream_cache_max_bytes:
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
        if stamp is None:
            try:
                stamp = self._receipt_stamp(self._path(sequence))
            except OSError:
                return
        self._stream_cache[sequence] = (
            time.monotonic(),
            stream,
            stream_hash,
            result_hash,
            int(result_bytes),
            stamp,
        )
        self._stream_cache_bytes += size

    def _stream_cache_get(
        self,
        sequence: str,
    ) -> tuple[bytes, str, str, int] | None:
        cached = self._stream_cache.get(sequence)
        if cached is None:
            return None
        created, stream, stream_hash, result_hash, result_bytes, stamp = cached
        try:
            unchanged = self._receipt_stamp(self._path(sequence)) == stamp
        except OSError:
            unchanged = False
        if time.monotonic() - created > self.ttl_seconds or not unchanged:
            self._stream_cache.pop(sequence, None)
            self._stream_cache_bytes -= len(stream)
            return None
        self._stream_cache.move_to_end(sequence)
        return stream, stream_hash, result_hash, result_bytes

    @staticmethod
    def _compress_result_stream_bounded(raw: bytes) -> bytes:
        """Encode at zlib level 1; empty bytes mark compression ineligible.

        JSON results up to 64 MiB may be essentially incompressible. Avoid
        retaining a second 64 MiB allocation when the stream-cache budget is
        only 16 MiB; the existing plain-chunk path remains available.
        """
        compressor = zlib.compressobj(level=1)
        stream = bytearray()
        for start in range(0, len(raw), 65536):
            part = compressor.compress(raw[start:start+65536])
            if len(stream) + len(part) > MAX_COMPRESSED_RESULT_STREAM_BYTES:
                return b""
            stream.extend(part)
        tail = compressor.flush()
        if len(stream) + len(tail) > MAX_COMPRESSED_RESULT_STREAM_BYTES:
            return b""
        stream.extend(tail)
        return bytes(stream)

    def _result_stream_payload(
        self,
        sequence: str,
    ) -> tuple[bytes, str, str, int] | None:
        with self._lock:
            cached = self._stream_cache_get(sequence)
            if cached is not None:
                return cached
        path = self._path(sequence)
        for _ in range(3):
            try:
                before = self._receipt_stamp(path)
            except FileNotFoundError:
                return None
            payload = self._result_payload(sequence)
            if payload is None:
                return None
            _, raw, result_hash = payload
            stream = self._compress_result_stream_bounded(raw)
            stream_hash = hashlib.sha256(stream).hexdigest()
            try:
                after = self._receipt_stamp(path)
            except FileNotFoundError:
                continue
            if before != after:
                continue
            with self._lock:
                self._stream_cache_put(
                    sequence, stream, stream_hash, result_hash, len(raw),
                    stamp=after,
                )
            return stream, stream_hash, result_hash, len(raw)
        raise RuntimeError("RESULT_RECEIPT_CHANGED_DURING_READ")

    @staticmethod
    def _validated_chunk_size(value: int) -> int:
        # The plain receipt path needs the same cheap preflight as EC2
        # streams: do not read/decompress a 64 MiB record only to discover
        # an absurd chunk-size parameter afterward.
        if (isinstance(value, bool) or
                not isinstance(value, (int, float, str)) or
                (isinstance(value, str) and len(value) > 32)):
            raise ValueError("INVALID_RESULT_CHUNK_BYTES")
        try:
            size = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("INVALID_RESULT_CHUNK_BYTES") from exc
        return max(256, min(4096, size))

    @staticmethod
    def _validated_batch_limit(value: int, error_code: str) -> int:
        # A caller-controlled text value must not trigger unbounded decimal
        # parsing before the chunk's own bounded result read.
        if (isinstance(value, bool) or
                not isinstance(value, (int, float, str)) or
                (isinstance(value, str) and len(value) > 32)):
            raise ValueError(error_code)
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(error_code) from exc
        return max(1, min(16, number))

    @staticmethod
    def _validated_stream_options(
        chunk_bytes: int, max_ratio: float,
    ) -> tuple[int, float]:
        # Validate cheap wire parameters before reading and recompressing a
        # potentially 64 MiB durable result. Keep the historical clamping
        # behavior for finite numeric values, including short numeric strings.
        if (isinstance(chunk_bytes, bool)
                or not isinstance(chunk_bytes, (int, float, str))
                or (isinstance(chunk_bytes, str) and len(chunk_bytes) > 32)):
            raise ValueError("INVALID_RESULT_STREAM_CHUNK_BYTES")
        try:
            size = int(chunk_bytes)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("INVALID_RESULT_STREAM_CHUNK_BYTES") from exc
        if (isinstance(max_ratio, bool)
                or not isinstance(max_ratio, (int, float, str))
                or (isinstance(max_ratio, str) and len(max_ratio) > 32)):
            raise ValueError("INVALID_RESULT_STREAM_MAX_RATIO")
        try:
            ratio = float(max_ratio)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("INVALID_RESULT_STREAM_MAX_RATIO") from exc
        if not math.isfinite(ratio):
            raise ValueError("INVALID_RESULT_STREAM_MAX_RATIO")
        return max(256, min(4096, size)), max(0.10, min(1.0, ratio))

    @staticmethod
    def _stream_metadata(
        sequence: str,
        payload: tuple[bytes, str, str, int],
        *,
        chunk_bytes: int,
        max_ratio: float,
    ) -> dict[str, Any]:
        # Metadata and chunk bytes must come from the *same* immutable
        # snapshot. A receipt can be atomically replaced by another process
        # between two separate lookups.
        stream, stream_hash, result_hash, result_bytes = payload
        size, ratio_limit = DurableResultStore._validated_stream_options(
            chunk_bytes, max_ratio)
        # Empty stream is the bounded compressor's ineligible sentinel,
        # never a successful zero-byte zlib payload.
        ratio = (len(stream) / result_bytes) if stream and result_bytes else 1.0
        eligible = bool(stream and result_bytes >= 4096 and ratio <= ratio_limit)
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

    def stream_meta(
        self,
        sequence: str,
        *,
        chunk_bytes: int = 2048,
        max_ratio: float = 0.85,
    ) -> dict[str, Any] | None:
        chunk_bytes, max_ratio = self._validated_stream_options(
            chunk_bytes, max_ratio)
        payload = self._result_stream_payload(sequence)
        if payload is None:
            return None
        return self._stream_metadata(
            sequence, payload, chunk_bytes=chunk_bytes, max_ratio=max_ratio,
        )

    def stream_chunks(
        self,
        sequence: str,
        indexes: list[int],
        *,
        chunk_bytes: int = 2048,
        max_chunks: int = 8,
        max_ratio: float = 0.85,
    ) -> dict[str, Any] | None:
        # Validate untrusted indexes before decoding and recompressing a
        # potentially multi-megabyte result. Rejecting a bogus request should
        # not consume the entire EC2 recovery CPU/memory budget.
        if not isinstance(indexes, list) or not indexes:
            raise ValueError("RESULT_STREAM_CHUNK_INDEXES_REQUIRED")
        chunk_bytes, max_ratio = self._validated_stream_options(
            chunk_bytes, max_ratio)
        limit = self._validated_batch_limit(
            max_chunks, "INVALID_RESULT_STREAM_MAX_CHUNKS")
        # The returned batch is capped at 16, but duplicate raw indexes could
        # otherwise make parsing cost grow without a bound. Check the raw
        # request before iterating, and deduplicate in O(1) per index.
        if len(indexes) > 256:
            raise ValueError("RESULT_STREAM_CHUNK_REQUEST_TOO_LARGE")
        unique: list[int] = []
        seen: set[int] = set()
        for raw_index in indexes:
            if (type(raw_index) is not int and
                    not (isinstance(raw_index, str) and len(raw_index) <= 32
                         and raw_index.strip().isdecimal())):
                # Bound text length *before* strip/isdecimal/int: a decimal
                # string with millions of digits can monopolize parsing CPU.
                raise ValueError("INVALID_RESULT_STREAM_CHUNK_INDEX")
            index = int(raw_index)
            # Reject impossible indexes before a full receipt read/decode.
            # Even a 64 MiB JSON receipt cannot create more chunks than this
            # conservative 65 MiB upper bound at the 256-byte minimum size.
            maximum_index=(MAX_DECODED_RECEIPT_BYTES + 1_048_576) // 256
            if not 0 <= index <= maximum_index:
                raise IndexError("RESULT_STREAM_CHUNK_OUT_OF_RANGE")
            if index not in seen:
                seen.add(index)
                unique.append(index)
                if len(unique) > limit:
                    raise ValueError(f"RESULT_STREAM_CHUNK_BATCH_LIMIT:{limit}")

        payload = self._result_stream_payload(sequence)
        if payload is None:
            return None
        meta = self._stream_metadata(
            sequence, payload, chunk_bytes=chunk_bytes, max_ratio=max_ratio,
        )
        if not meta["eligible"]:
            return {**meta, "requested_indexes": [], "chunks": [], "complete": False}

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
            [index],
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
        size = self._validated_chunk_size(chunk_bytes)
        payload = self._result_payload(sequence)
        if payload is None:
            return None
        _, raw, digest = payload
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
        limit = self._validated_batch_limit(
            max_chunks, "INVALID_RESULT_MAX_CHUNKS")
        # An unbounded duplicate request must not do quadratic list searches
        # before noticing that only 16 distinct chunks can be returned.
        if len(indexes) > 256:
            raise ValueError("RESULT_CHUNK_REQUEST_TOO_LARGE")
        unique: list[int] = []
        seen: set[int] = set()
        for raw_index in indexes:
            if (type(raw_index) is not int and
                    not (isinstance(raw_index, str) and len(raw_index) <= 32
                         and raw_index.strip().isdecimal())):
                # Bound text length *before* strip/isdecimal/int: a decimal
                # string with millions of digits can monopolize parsing CPU.
                raise ValueError("INVALID_RESULT_CHUNK_INDEX")
            index = int(raw_index)
            # Reject impossible indexes before a full receipt read/decode.
            # Even a 64 MiB JSON receipt cannot create more chunks than this
            # conservative 65 MiB upper bound at the 256-byte minimum size.
            maximum_index=(MAX_DECODED_RECEIPT_BYTES + 1_048_576) // 256
            if not 0 <= index <= maximum_index:
                raise IndexError("RESULT_CHUNK_OUT_OF_RANGE")
            if index not in seen:
                seen.add(index)
                unique.append(index)
                if len(unique) > limit:
                    raise ValueError(f"RESULT_CHUNK_BATCH_LIMIT:{limit}")

        size = self._validated_chunk_size(chunk_bytes)
        payload = self._result_payload(sequence)
        if payload is None:
            return None
        _, raw, result_hash = payload
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
        # The single-chunk shortcut must enforce the same bounded integer
        # format as chunks() before decoding the underlying receipt.
        if (type(index) is not int and
                not (isinstance(index, str) and len(index) <= 32
                     and index.strip().isdecimal())):
            raise ValueError("INVALID_RESULT_CHUNK_INDEX")
        index = int(index)
        # The single-index endpoint is used for selective EC2 recovery.
        # Reject clearly impossible offsets before loading a large receipt,
        # exactly as the multi-index path already does.
        maximum_index = (MAX_DECODED_RECEIPT_BYTES + 1_048_576) // 256
        if index < 0 or index > maximum_index:
            raise IndexError("RESULT_CHUNK_OUT_OF_RANGE")
        size = self._validated_chunk_size(chunk_bytes)
        payload = self._result_payload(sequence)
        if payload is None:
            return None
        _, raw, digest = payload
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
