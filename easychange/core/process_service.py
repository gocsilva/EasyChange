from __future__ import annotations
import hashlib
import json
import os

import re
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass


_DIAGNOSTIC_RE = re.compile(
    r"(?:\berror\b|\bwarning\b|\bfailed\b|\bfailure\b|\bexception\b|"
    r"\btraceback\b|\bCS\d{4}\b|\bNU\d{4}\b)",
    re.IGNORECASE,
)


@dataclass
class RunningProcess:
    process_id: str
    argv: list[str]
    process: subprocess.Popen
    stdout_path: Path
    stderr_path: Path
    redact_values: tuple[str, ...] = ()


def _redact(text: str, values) -> str:
    output = str(text or "")
    for value in values or ():
        secret = str(value or "")
        if secret:
            output = output.replace(secret, "[REDACTED]")
    return output


def _diagnostics(stdout: str, stderr: str, limit: int = 12000) -> str:
    lines = []
    seen = set()
    for line in (stdout + "\n" + stderr).splitlines():
        if not _DIAGNOSTIC_RE.search(line):
            continue
        compact = line.strip()
        if compact and compact not in seen:
            seen.add(compact)
            lines.append(compact)
    return "\n".join(lines[-200:])[-limit:]


class ProcessService:
    JOB_TTL_SECONDS = 24 * 60 * 60
    LOG_TTL_SECONDS = 6 * 60 * 60
    SCRIPT_TTL_SECONDS = 7 * 24 * 60 * 60
    MAX_DURABLE_JOBS = 128
    DEFAULT_MAX_RUNTIME_BYTES = 256 * 1024 * 1024

    @staticmethod
    def _runtime_budget() -> int:
        try:
            value = int(
                os.environ.get("EASYCHANGE_MAX_RUNTIME_BYTES")
                or ProcessService.DEFAULT_MAX_RUNTIME_BYTES
            )
        except (TypeError, ValueError):
            value = ProcessService.DEFAULT_MAX_RUNTIME_BYTES
        return max(1024 * 1024, min(16 * 1024 * 1024 * 1024, value))

    def __init__(self, cwd: Path) -> None:
        self.cwd = Path(cwd).resolve()
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "EasyChange" / "Workspaces"
        try:
            stat = self.cwd.stat()
            workspace_identity = (
                f"{str(self.cwd).casefold()}|{int(getattr(stat, 'st_dev', 0))}|"
                f"{int(getattr(stat, 'st_ino', 0))}"
            )
        except OSError:
            workspace_identity = str(self.cwd).casefold()
        workspace_hash = hashlib.sha256(
            workspace_identity.encode("utf-8", errors="replace")
        ).hexdigest()[:20]
        self.workspace_state_root = base / workspace_hash
        self.root = self.workspace_state_root / "processes"
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_root = self.workspace_state_root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.max_runtime_bytes = self._runtime_budget()
        self._migrate_legacy_jobs()
        self._processes: dict[str, RunningProcess] = {}
        self.last_execution: dict | None = None

    def _migrate_legacy_jobs(self) -> None:
        legacy = self.cwd / ".easychange" / "jobs"
        if not legacy.is_dir():
            return
        try:
            candidates = list(legacy.glob("*.json"))
        except OSError:
            return
        for source in candidates[:1024]:
            target = self.jobs_root / source.name
            if target.exists():
                continue
            try:
                target.write_bytes(source.read_bytes())
            except OSError:
                continue

    def _job_path(self, job_id: str) -> Path:
        safe = "".join(ch for ch in str(job_id) if ch.isalnum() or ch in "_-")
        if not safe or safe != str(job_id):
            raise ValueError("INVALID_JOB_ID")
        return self.jobs_root / f"{safe}.json"

    def _write_job(self, job_id: str, data: dict) -> dict:
        target = self._job_path(job_id)
        temporary = target.with_suffix(".json.tmp")
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
        return data

    def _read_job(self, job_id: str) -> dict | None:
        target = self._job_path(job_id)
        if not target.exists():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
        except OSError:
            return hashlib.sha256(b"").hexdigest()
        return digest.hexdigest()
    @staticmethod
    def _tail_file(path: Path, limit: int) -> tuple[str, int, str]:
        try:
            with path.open("rb") as handle:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - max(512, int(limit))))
                payload = handle.read()
            return payload.decode("utf-8", errors="replace"), size, ProcessService._hash_file(path)
        except OSError:
            return "", 0, hashlib.sha256(b"").hexdigest()

    def _finalize_job(self, job_id: str, item: RunningProcess, *, cancelled: bool = False) -> dict:
        current = self._read_job(job_id) or {}
        code = item.process.poll()
        if code is None:
            return current
        stdout, stdout_size, stdout_hash = self._tail_file(item.stdout_path, 32000 if code == 0 else 64000)
        stderr, stderr_size, stderr_hash = self._tail_file(item.stderr_path, 32000 if code == 0 else 64000)
        stdout = _redact(stdout, item.redact_values)
        stderr = _redact(stderr, item.redact_values)
        state = "CANCELLED" if cancelled else ("SUCCEEDED" if code == 0 else "FAILED")
        finished_at = time.time()
        started_at = float(current.get("started_at") or finished_at)
        result = {
            **current, "job_id": job_id, "process_id": item.process_id, "pid": item.process.pid,
            "argv": item.argv, "state": state, "returncode": code, "finished_at": finished_at,
            "duration_ms": int(max(0.0, finished_at - started_at) * 1000),
            "stdout": stdout, "stderr": stderr, "stdout_chars": stdout_size, "stderr_chars": stderr_size,
            "stdout_bytes": stdout_size, "stderr_bytes": stderr_size,
            "stdout_hash": stdout_hash, "stderr_hash": stderr_hash,
            "diagnostics": _diagnostics(stdout, stderr),
            "result_hash": hashlib.sha256((stdout_hash + ":" + stderr_hash + ":" + str(code)).encode("ascii")).hexdigest(),
            "output_truncated": stdout_size > len(stdout.encode("utf-8", errors="replace"))
                                or stderr_size > len(stderr.encode("utf-8", errors="replace")),
            "terminal": True,
        }
        self._write_job(job_id, result)
        return result

    def job_start(
        self,
        argv: list[str],
        job_id: str,
        *,
        env: dict[str, str] | None = None,
        redact_values: list[str] | tuple[str, ...] | None = None,
        metadata: dict | None = None,
    ) -> dict:
        previous = self._read_job(job_id)
        if previous is not None:
            if previous.get("argv") != argv:
                raise RuntimeError("JOB_ID_CONFLICT")
            return self.job_status(job_id)
        started = self.start(argv, job_id, env=env, redact_values=redact_values)
        record = {
            "job_id": job_id,
            "process_id": job_id,
            "pid": started["pid"],
            "argv": list(argv),
            "state": "RUNNING",
            "created_at": time.time(),
            "started_at": time.time(),
            "finished_at": None,
            "returncode": None,
            "metadata": dict(metadata or {}),
        }
        return self._write_job(job_id, record)

    def job_status(self, job_id: str) -> dict:
        current = self._read_job(job_id)
        item = self._processes.get(job_id)
        if item is not None:
            code = item.process.poll()
            if code is None:
                if current is None:
                    current = {"job_id": job_id, "process_id": job_id, "pid": item.process.pid,
                               "argv": item.argv, "state": "RUNNING", "started_at": time.time()}
                    self._write_job(job_id, current)
                snapshot = {k: v for k, v in current.items() if k not in {"stdout", "stderr"}}
                started = float(snapshot.get("started_at") or time.time())
                snapshot["duration_ms"] = int(max(0.0, time.time() - started) * 1000)
                snapshot["terminal"] = False
                return snapshot
            current = self._finalize_job(job_id, item)
        if current is None:
            raise LookupError(f"Job not found: {job_id}")
        snapshot = {k: v for k, v in current.items() if k not in {"stdout", "stderr"}}
        snapshot.setdefault("terminal", snapshot.get("state") != "RUNNING")
        return snapshot

    def job_result(self, job_id: str) -> dict:
        item = self._processes.get(job_id)
        if item is not None and item.process.poll() is not None:
            self._finalize_job(job_id, item)
        current = self._read_job(job_id)
        if current is None:
            raise LookupError(f"Job not found: {job_id}")
        if current.get("state") == "RUNNING":
            started = float(current.get("started_at") or time.time())
            return {"job_id": job_id, "state": "RUNNING", "pid": current.get("pid"),
                    "started_at": current.get("started_at"),
                    "duration_ms": int(max(0.0, time.time() - started) * 1000), "terminal": False}
        result = dict(current)
        result.setdefault("terminal", True)
        return result

    def job_logs(
        self,
        job_id: str,
        *,
        stream: str = "stdout",
        cursor: int = 0,
        max_bytes: int = 16384,
    ) -> dict:
        stream = str(stream or "stdout").casefold()
        if stream not in {"stdout", "stderr"}:
            raise ValueError("JOB_LOG_STREAM_MUST_BE_STDOUT_OR_STDERR")
        cursor = max(0, int(cursor or 0))
        page_bytes = max(256, min(1024 * 1024, int(max_bytes or 16384)))

        current = self._read_job(job_id)
        item = self._processes.get(job_id)
        if current is None and item is None:
            raise LookupError(f"Job not found: {job_id}")

        path = (
            (item.stdout_path if stream == "stdout" else item.stderr_path)
            if item is not None
            else self.root / f"{job_id}.{('out' if stream == 'stdout' else 'err')}"
        )
        redact_values = item.redact_values if item is not None else ()
        if path.exists():
            try:
                with path.open("rb") as handle:
                    total = handle.seek(0, 2)
                    start = min(cursor, total)
                    handle.seek(start)
                    raw = handle.read(page_bytes)
            except OSError:
                raw = b""
                total = 0
                start = 0
            text = _redact(raw.decode("utf-8", errors="replace"), redact_values)
            consumed = len(raw)
            next_cursor = start + consumed
            complete = next_cursor >= total
            return {
                "job_id": job_id,
                "stream": stream,
                "cursor": start,
                "next_cursor": None if complete else next_cursor,
                "returned_bytes": consumed,
                "total_bytes": total,
                "remaining_bytes": max(0, total - next_cursor),
                "complete": complete,
                "text": text,
                "durable_file": True,
                "terminal": bool(current and current.get("state") != "RUNNING"),
            }

        # Compatibility fallback for old durable job records that only retained a tail.
        text = str((current or {}).get(stream) or "")
        raw = text.encode("utf-8", errors="replace")
        start = min(cursor, len(raw))
        chunk = raw[start:start + page_bytes]
        next_cursor = start + len(chunk)
        complete = next_cursor >= len(raw)
        return {
            "job_id": job_id,
            "stream": stream,
            "cursor": start,
            "next_cursor": None if complete else next_cursor,
            "returned_bytes": len(chunk),
            "total_bytes": len(raw),
            "remaining_bytes": max(0, len(raw) - next_cursor),
            "complete": complete,
            "text": chunk.decode("utf-8", errors="replace"),
            "durable_file": False,
            "tail_fallback": True,
            "terminal": bool(current and current.get("state") != "RUNNING"),
        }

    def job_cancel(self, job_id: str) -> dict:
        item = self._processes.get(job_id)
        if item is None:
            current = self._read_job(job_id)
            if current is None:
                raise LookupError(f"Job not found: {job_id}")
            return current
        if item.process.poll() is None:
            item.process.terminate()
            try:
                item.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                item.process.kill()
                item.process.wait(timeout=3)
        return self._finalize_job(job_id, item, cancelled=True)
    def _prune_finished(self, keep: int = 32) -> None:
        """Bound process objects; durable jobs are finalized before log cleanup."""
        keep = max(0, int(keep))
        finished = [
            process_id
            for process_id, item in self._processes.items()
            if item.process.poll() is not None
        ]
        for process_id in finished:
            item = self._processes.get(process_id)
            if item is not None and self._job_path(process_id).exists():
                self._finalize_job(process_id, item)
        excess = finished[:-keep] if keep else finished
        for process_id in excess:
            item = self._processes.pop(process_id, None)
            if item is None:
                continue
            # Durable jobs keep their external log files for cursor-based retrieval.
            # Non-job processes can still release their transient files immediately.
            if not self._job_path(process_id).exists():
                for path in (item.stdout_path, item.stderr_path):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return int(path.stat().st_size) if path.is_file() else 0
        except OSError:
            return 0

    def storage_stats(self) -> dict:
        total = 0
        files = 0
        try:
            items = list(self.workspace_state_root.rglob("*"))
        except OSError:
            items = []
        for path in items:
            if not path.is_file() or path.is_symlink():
                continue
            total += self._file_size(path)
            files += 1
        return {
            "root": str(self.workspace_state_root),
            "bytes": total,
            "files": files,
            "max_bytes": self.max_runtime_bytes,
            "over_budget": total > self.max_runtime_bytes,
        }

    def _safe_external_unlink(self, path: Path) -> int:
        candidate = Path(os.path.abspath(path))
        root = Path(os.path.abspath(self.workspace_state_root))
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"REFUSING_NON_EASYCHANGE_RUNTIME_PATH:{candidate}") from exc
        size = self._file_size(candidate)
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            return 0
        return size

    def _durable_job_state(self, path: Path) -> str:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return str(value.get("state") or "").upper() if isinstance(value, dict) else ""
        except (OSError, json.JSONDecodeError):
            return "CORRUPT"

    def maintenance(self, keep_finished: int = 8) -> dict:
        before_count = len(self._processes)
        self._prune_finished(keep=max(0, int(keep_finished)))
        active = {
            process_id
            for process_id, item in self._processes.items()
            if item.process.poll() is None
        }
        running = len(active)
        before_storage = self.storage_stats()
        now = time.time()
        removed_files = 0
        reclaimed_bytes = 0

        # Terminal job manifests are bounded by age/count. A RUNNING manifest is
        # never removed merely because the Python process map was lost.
        try:
            jobs = sorted(
                self.jobs_root.glob("*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            jobs = []
        terminal_position = 0
        for path in jobs:
            state = self._durable_job_state(path)
            if state == "RUNNING" or path.stem in active:
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                age = self.JOB_TTL_SECONDS + 1
            if terminal_position >= self.MAX_DURABLE_JOBS or age > self.JOB_TTL_SECONDS:
                for target in (
                    path,
                    self.root / f"{path.stem}.out",
                    self.root / f"{path.stem}.err",
                ):
                    size = self._safe_external_unlink(target)
                    if size:
                        removed_files += 1
                        reclaimed_bytes += size
            terminal_position += 1

        # Orphan/inactive log files have a shorter TTL.
        try:
            logs = [
                path for path in self.root.glob("*.*")
                if path.suffix.lower() in {".out", ".err"}
            ]
        except OSError:
            logs = []
        for path in logs:
            if path.stem in active:
                continue
            job_manifest = self.jobs_root / f"{path.stem}.json"
            if job_manifest.exists() and self._durable_job_state(job_manifest) == "RUNNING":
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                age = self.LOG_TTL_SECONDS + 1
            if age > self.LOG_TTL_SECONDS:
                size = self._safe_external_unlink(path)
                if size:
                    removed_files += 1
                    reclaimed_bytes += size

        # Generated EF scripts are EasyChange-owned, external to the repo, and
        # disposable after their retention window.
        script_root = self.workspace_state_root / "ef-scripts"
        try:
            scripts = [path for path in script_root.glob("*.sql") if path.is_file()]
        except OSError:
            scripts = []
        for path in scripts:
            try:
                age = now - path.stat().st_mtime
            except OSError:
                age = self.SCRIPT_TTL_SECONDS + 1
            if age > self.SCRIPT_TTL_SECONDS:
                size = self._safe_external_unlink(path)
                if size:
                    removed_files += 1
                    reclaimed_bytes += size

        # Byte-pressure cleanup: only terminal jobs and inactive logs, oldest
        # first. Running jobs/processes are preserved even if the quota remains
        # exceeded.
        current = self.storage_stats()["bytes"]
        candidates: list[tuple[float, list[Path]]] = []
        try:
            jobs = list(self.jobs_root.glob("*.json"))
        except OSError:
            jobs = []
        grouped_stems: set[str] = set()
        for path in jobs:
            if self._durable_job_state(path) == "RUNNING" or path.stem in active:
                continue
            grouped_stems.add(path.stem)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            candidates.append((
                mtime,
                [
                    path,
                    self.root / f"{path.stem}.out",
                    self.root / f"{path.stem}.err",
                ],
            ))
        try:
            logs = list(self.root.glob("*.*"))
        except OSError:
            logs = []
        for path in logs:
            if path.suffix.lower() not in {".out", ".err"}:
                continue
            if path.stem in grouped_stems or path.stem in active:
                continue
            manifest = self.jobs_root / f"{path.stem}.json"
            if manifest.exists() and self._durable_job_state(manifest) == "RUNNING":
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            candidates.append((mtime, [path]))

        try:
            scripts = [
                path for path in (self.workspace_state_root / "ef-scripts").glob("*.sql")
                if path.is_file()
            ]
        except OSError:
            scripts = []
        for path in scripts:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            candidates.append((mtime, [path]))

        for _mtime, group in sorted(candidates, key=lambda item: item[0]):
            if current <= self.max_runtime_bytes:
                break
            for path in group:
                size = self._safe_external_unlink(path)
                if size:
                    current = max(0, current - size)
                    removed_files += 1
                    reclaimed_bytes += size

        after_storage = self.storage_stats()
        return {
            "tracked_before": before_count,
            "tracked_after": len(self._processes),
            "running": running,
            "finished_retained": len(self._processes) - running,
            "disk_before_bytes": before_storage["bytes"],
            "disk_after_bytes": after_storage["bytes"],
            "disk_max_bytes": self.max_runtime_bytes,
            "disk_over_budget": after_storage["bytes"] > self.max_runtime_bytes,
            "disk_reclaimed_bytes": reclaimed_bytes,
            "disk_removed_files": removed_files,
        }

    def purge_durable_state(self) -> dict:
        """Remove only this workspace's external EasyChange runtime state."""
        if any(item.process.poll() is None for item in self._processes.values()):
            raise RuntimeError("CANNOT_PURGE_RUNNING_EASYCHANGE_PROCESSES")
        root = Path(os.path.abspath(self.workspace_state_root))
        base = Path(
            os.path.abspath(
                Path(os.environ.get("LOCALAPPDATA") or Path.home())
                / "EasyChange"
                / "Workspaces"
            )
        )
        try:
            root.relative_to(base)
        except ValueError as exc:
            raise ValueError(f"REFUSING_NON_EASYCHANGE_RUNTIME_ROOT:{root}") from exc

        before = self.storage_stats()
        file_count = 0
        if root.exists():
            try:
                file_count = sum(1 for path in root.rglob("*") if path.is_file())
            except OSError:
                file_count = 0
            try:
                import shutil
                shutil.rmtree(root)
            except OSError as exc:
                return {
                    "ok": False,
                    "root": str(root),
                    "removed_files": 0,
                    "reclaimed_bytes": 0,
                    "error": str(exc)[:300],
                }
        return {
            "ok": True,
            "root": str(root),
            "removed_files": file_count,
            "reclaimed_bytes": int(before.get("bytes") or 0),
        }

    def active_process_ids(self) -> set[str]:
        return {
            process_id
            for process_id, item in self._processes.items()
            if item.process.poll() is None
        }

    def run(
        self,
        argv: list[str],
        timeout: int = 300,
        *,
        env: dict[str, str] | None = None,
        redact_values: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        """Run a command without buffering unbounded stdout/stderr in RAM."""
        if not argv:
            raise ValueError("Command is empty")

        stamp = f"run-{time.time_ns()}"
        stdout_path = self.root / f"{stamp}.out"
        stderr_path = self.root / f"{stamp}.err"
        started = time.monotonic()

        try:
            with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
                completed = subprocess.run(
                    argv,
                    cwd=self.cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=timeout,
                    check=False,
                    shell=False,
                    env=env,
                )

            stdout_size = stdout_path.stat().st_size if stdout_path.exists() else 0
            stderr_size = stderr_path.stat().st_size if stderr_path.exists() else 0
            tail_limit = 4000 if completed.returncode == 0 else 12000

            def tail(path: Path, limit: int) -> str:
                try:
                    with path.open("rb") as handle:
                        size = handle.seek(0, 2)
                        handle.seek(max(0, size - limit))
                        return handle.read().decode("utf-8", errors="replace")
                except OSError:
                    return ""

            diagnostics: list[str] = []
            seen: set[str] = set()
            for path in (stdout_path, stderr_path):
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        for line in handle:
                            if not _DIAGNOSTIC_RE.search(line):
                                continue
                            compact = line.strip()
                            if compact and compact not in seen:
                                seen.add(compact)
                                diagnostics.append(compact)
                                if len(diagnostics) > 400:
                                    diagnostics = diagnostics[-200:]
                except OSError:
                    continue

            stdout = _redact(tail(stdout_path, tail_limit), redact_values)
            stderr = _redact(tail(stderr_path, tail_limit), redact_values)
            diagnostics_text = _redact("\n".join(diagnostics[-200:])[-12000:], redact_values)
            self.last_execution = {
                "argv": argv,
                "returncode": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "diagnostics": diagnostics_text,
                "stdout_chars": stdout_size,
                "stderr_chars": stderr_size,
                "stdout_bytes": stdout_size,
                "stderr_bytes": stderr_size,
                "output_truncated": stdout_size > len(stdout.encode("utf-8", errors="replace"))
                                    or stderr_size > len(stderr.encode("utf-8", errors="replace")),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "capture_backend": "tempfile-tail",
            }
            return self.last_execution
        finally:
            for path in (stdout_path, stderr_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def start(
        self,
        argv: list[str],
        process_id: str,
        *,
        env: dict[str, str] | None = None,
        redact_values: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        if not argv:
            raise ValueError("Command is empty")
        self._prune_finished()
        existing = self._processes.get(process_id)
        if existing is not None and existing.process.poll() is None:
            raise RuntimeError(f"Process already running: {process_id}")
        stdout_path, stderr_path = self.root / f"{process_id}.out", self.root / f"{process_id}.err"
        stdout_file, stderr_file = stdout_path.open("wb"), stderr_path.open("wb")
        try:
            proc = subprocess.Popen(
                argv, cwd=self.cwd, stdin=subprocess.DEVNULL,
                stdout=stdout_file, stderr=stderr_file, shell=False,
                env=env,
            )
        finally:
            stdout_file.close()
            stderr_file.close()
        self._processes[process_id] = RunningProcess(
            process_id,
            argv,
            proc,
            stdout_path,
            stderr_path,
            tuple(str(value) for value in (redact_values or ()) if str(value)),
        )
        return {"process_id": process_id, "pid": proc.pid, "state": "RUNNING", "argv": argv}

    def list(self) -> list[dict]:
        self._prune_finished()
        output = []
        seen = set()
        for key, item in list(self._processes.items()):
            code = item.process.poll()
            seen.add(key)
            output.append({
                "process_id": key,
                "pid": item.process.pid,
                "state": "RUNNING" if code is None else "EXITED",
                "returncode": code,
                "argv": item.argv,
            })
        for path in sorted(self.jobs_root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:128]:
            job_id = path.stem
            if job_id in seen:
                continue
            current = self._read_job(job_id)
            if current:
                output.append({
                    "process_id": job_id,
                    "job_id": job_id,
                    "pid": current.get("pid"),
                    "state": current.get("state"),
                    "returncode": current.get("returncode"),
                    "argv": current.get("argv"),
                })
        return output

    def stop(self, process_id: str) -> dict:
        item = self._processes.get(process_id)
        if item is None:
            raise LookupError(f"Process not found: {process_id}")
        if item.process.poll() is None:
            item.process.terminate()
            try:
                item.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                item.process.kill()
                item.process.wait(timeout=3)
        result = {"process_id": process_id, "state": "STOPPED", "returncode": item.process.returncode}
        self._prune_finished()
        return result

    def logs(self, process_id: str, limit: int = 12000) -> dict:
        item = self._processes.get(process_id)
        size_limit = max(512, int(limit))
        if item is None:
            current = self._read_job(process_id)
            if current is None:
                raise LookupError(f"Process not found: {process_id}")
            stdout = str(current.get("stdout") or "")
            stderr = str(current.get("stderr") or "")
            return {
                "process_id": process_id, "job_id": current.get("job_id") or process_id,
                "state": current.get("state"), "returncode": current.get("returncode"),
                "stdout": stdout[-size_limit:], "stderr": stderr[-size_limit:],
                "stdout_bytes": current.get("stdout_bytes", current.get("stdout_chars")),
                "stderr_bytes": current.get("stderr_bytes", current.get("stderr_chars")),
                "output_truncated": bool(current.get("output_truncated")) or len(stdout) > size_limit or len(stderr) > size_limit,
                "durable": True, "terminal": current.get("state") != "RUNNING",
            }
        def tail(path: Path) -> tuple[str, int]:
            try:
                with path.open("rb") as handle:
                    size = handle.seek(0, 2)
                    handle.seek(max(0, size - size_limit))
                    return handle.read().decode("utf-8", errors="replace"), size
            except OSError:
                return "", 0
        stdout, stdout_size = tail(item.stdout_path)
        stderr, stderr_size = tail(item.stderr_path)
        stdout = _redact(stdout, item.redact_values)
        stderr = _redact(stderr, item.redact_values)
        running = item.process.poll() is None
        return {
            "process_id": process_id, "state": "RUNNING" if running else "EXITED",
            "returncode": item.process.poll(), "stdout": stdout, "stderr": stderr,
            "stdout_bytes": stdout_size, "stderr_bytes": stderr_size,
            "output_truncated": stdout_size > len(stdout.encode("utf-8", errors="replace"))
                                or stderr_size > len(stderr.encode("utf-8", errors="replace")),
            "durable": False, "terminal": not running,
        }

    def stop_all(self) -> None:
        for process_id, item in list(self._processes.items()):
            if item.process.poll() is None:
                self.stop(process_id)
        # CommandService is closing; background process logs are ephemeral.
        self._prune_finished(keep=0)
