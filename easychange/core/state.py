from __future__ import annotations

import json
import math
import os
import secrets
import tempfile
from pathlib import Path


MAX_MACHINE_SESSION_BYTES = 16 * 1024 * 1024


def _finite_session_number(token: str) -> float:
    number = float(token)
    if not math.isfinite(number):
        raise ValueError("MACHINE_SESSION_NONFINITE_JSON")
    return number


def _reject_session_constant(_token: str):
    raise ValueError("MACHINE_SESSION_NONFINITE_JSON")


def _bounded_session_json(value: dict) -> bytes:
    """Do not create a session file that the bounded loader cannot reopen."""
    output = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    for fragment in encoder.iterencode(value):
        if len(fragment) > MAX_MACHINE_SESSION_BYTES - len(output):
            raise ValueError("MACHINE_SESSION_TOO_LARGE")
        for offset in range(0, len(fragment), 4096):
            piece = fragment[offset:offset + 4096].encode("utf-8")
            if len(output) + len(piece) > MAX_MACHINE_SESSION_BYTES:
                raise ValueError("MACHINE_SESSION_TOO_LARGE")
            output.extend(piece)
    return bytes(output)


class MachineSession:
    """Small persisted view/session record; persisted atomically under .easychange."""
    def __init__(self, path: Path, workspace: str) -> None:
        self.path = path
        self.data = {"session_id": "S" + secrets.token_hex(3).upper(), "workspace": workspace,
                     "current_file": None, "line": 1, "output_mode": "text", "transport": "local",
                     "history": [], "aliases": {"o": "open", "r": "read", "s": "search", "b": "build", "t": "test", "d": "diff"}}
        if path.exists():
            try:
                # A corrupt/imported JSON file must not allocate unlimited
                # memory before workspace startup and user-facing diagnostics.
                with path.open("rb") as stream:
                    raw = stream.read(MAX_MACHINE_SESSION_BYTES + 1)
                if len(raw) > MAX_MACHINE_SESSION_BYTES:
                    raise ValueError("MACHINE_SESSION_TOO_LARGE")
                loaded = json.loads(
                    raw.decode("utf-8"), parse_constant=_reject_session_constant,
                    parse_float=_finite_session_number)
                if isinstance(loaded, dict) and loaded.get("workspace") == workspace:
                    # A valid JSON object is not necessarily a valid session.
                    # Corrupt or externally edited nested fields must not
                    # crash CommandService, FileService or IdFactory startup.
                    # Keep the disk file untouched for recovery/diagnosis.
                    safe = dict(loaded)
                    if (type(safe.get("session_id")) is not str or
                            not 1 <= len(safe["session_id"]) <= 128):
                        safe.pop("session_id", None)
                    aliases = safe.get("aliases")
                    if (type(aliases) is not dict or
                            any(type(k) is not str or type(v) is not str
                                for k, v in aliases.items())):
                        safe.pop("aliases", None)
                    history = safe.get("history")
                    if (type(history) is not list or
                            any(type(item) is not str for item in history)):
                        safe.pop("history", None)
                    counts = safe.get("id_counts")
                    if counts is not None and (
                            type(counts) is not dict or
                            any(type(k) is not str or type(v) is not int or v < 0
                                for k, v in counts.items())):
                        safe.pop("id_counts", None)
                    hashes = safe.get("file_hashes")
                    if hashes is not None and (
                            type(hashes) is not dict or
                            any(type(k) is not str or type(v) is not str
                                for k, v in hashes.items())):
                        safe.pop("file_hashes", None)
                    # Other structures are converted with dict() during
                    # CommandService construction. A JSON list/string may
                    # otherwise raise before the UI can report startup health.
                    field_rules = {
                        "results": lambda v: all(
                            type(k) is str and type(item) is dict
                            for k, item in v.items()),
                        "file_refs": lambda v: all(
                            type(k) is str and type(item) is str
                            for k, item in v.items()),
                        "macros": lambda v: all(
                            type(k) is str and type(item) is list and
                            all(type(line) is str for line in item)
                            for k, item in v.items()),
                        "snapshots": lambda v: all(
                            type(k) is str and type(item) is int and item >= 0
                            for k, item in v.items()),
                    }
                    for field, valid_items in field_rules.items():
                        value = safe.get(field)
                        if value is not None and (
                                type(value) is not dict or
                                not valid_items(value)):
                            safe.pop(field, None)
                    self.data.update(safe)
            except (ValueError, OSError, UnicodeError, RecursionError):
                # Preserve the original file for diagnosis and start with a
                # fresh in-memory session instead of crashing workspace load.
                pass
        path.parent.mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        payload = _bounded_session_json(self.data)
        last_error: OSError | None = None
        for _attempt in range(2):
            temporary: Path | None = None
            descriptor: int | None = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Parallel EasyChange instances must never share a .tmp name:
                # each write gets its own staging file on the same volume.
                descriptor, name = tempfile.mkstemp(
                    prefix=f".{self.path.name}.", suffix=".tmp",
                    dir=self.path.parent)
                temporary = Path(name)
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = None
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                if os.name == "posix":
                    # fsync(file) persists bytes, but not necessarily the new
                    # directory entry installed by atomic rename on power loss.
                    # Filesystems lacking directory fsync retain atomic replace.
                    directory_fd = None
                    try:
                        directory_fd = os.open(
                            self.path.parent,
                            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                        os.fsync(directory_fd)
                    except OSError:
                        pass
                    finally:
                        if directory_fd is not None:
                            os.close(directory_fd)
                return
            except FileNotFoundError as exc:
                last_error = exc
                continue
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        if last_error is not None:
            raise last_error
