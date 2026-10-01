from __future__ import annotations

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
        self._processes: dict[str, RunningProcess] = {}
        self.last_execution: dict | None = None

    def _prune_finished(self, keep: int = 32) -> None:
        """Bound exited-process metadata and ephemeral log files."""
        keep = max(0, int(keep))
        finished = [
            process_id
            for process_id, item in self._processes.items()
            if item.process.poll() is not None
        ]
        excess = finished[:-keep] if keep else finished
        for process_id in excess:
            item = self._processes.pop(process_id, None)
            if item is None:
                continue
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

            self.last_execution = {
                "argv": argv,
                "returncode": completed.returncode,
                "stdout": tail(stdout_path, tail_limit),
                "stderr": tail(stderr_path, tail_limit),
                "diagnostics": "\n".join(diagnostics[-200:])[-12000:],
                "stdout_chars": stdout_size,
                "stderr_chars": stderr_size,
                "output_truncated": stdout_size > tail_limit or stderr_size > tail_limit,
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
        for key, item in list(self._processes.items()):
            code = item.process.poll()
            output.append({
                "process_id": key,
                "pid": item.process.pid,
                "state": "RUNNING" if code is None else "EXITED",
                "returncode": code,
                "argv": item.argv,
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
        if item is None:
            raise LookupError(f"Process not found: {process_id}")
        def tail(path: Path) -> str:
            try:
                size_limit = max(512, int(limit))
                with path.open("rb") as handle:
                    size = handle.seek(0, 2)
                    handle.seek(max(0, size - size_limit))
                    return handle.read().decode("utf-8", errors="replace")
            except OSError:
                return ""
        return {
            "process_id": process_id,
            "state": "RUNNING" if item.process.poll() is None else "EXITED",
            "returncode": item.process.poll(),
            "stdout": tail(item.stdout_path),
            "stderr": tail(item.stderr_path),
        }

    def stop_all(self) -> None:
        for process_id, item in list(self._processes.items()):
            if item.process.poll() is None:
                self.stop(process_id)
        # CommandService is closing; background process logs are ephemeral.
        self._prune_finished(keep=0)
