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
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.root = cwd / ".easychange" / "processes"
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_root = cwd / ".easychange" / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._processes: dict[str, RunningProcess] = {}
        self.last_execution: dict | None = None

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

    def job_start(self, argv: list[str], job_id: str) -> dict:
        previous = self._read_job(job_id)
        if previous is not None:
            if previous.get("argv") != argv:
                raise RuntimeError("JOB_ID_CONFLICT")
            return self.job_status(job_id)
        started = self.start(argv, job_id)
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
            # Terminal job metadata already contains bounded output + hashes.
            for path in (item.stdout_path, item.stderr_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
    def run(self, argv: list[str], timeout: int = 300) -> dict:
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

            stdout = tail(stdout_path, tail_limit)
            stderr = tail(stderr_path, tail_limit)
            self.last_execution = {
                "argv": argv,
                "returncode": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "diagnostics": "\n".join(diagnostics[-200:])[-12000:],
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

    def start(self, argv: list[str], process_id: str) -> dict:
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
            )
        finally:
            stdout_file.close()
            stderr_file.close()
        self._processes[process_id] = RunningProcess(process_id, argv, proc, stdout_path, stderr_path)
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
