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

    def run(self, argv: list[str], timeout: int = 300) -> dict:
        if not argv:
            raise ValueError("Command is empty")
        started = time.monotonic()
        completed = subprocess.run(
            argv, cwd=self.cwd, text=True, capture_output=True,
            timeout=timeout, check=False, shell=False,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        tail_limit = 4000 if completed.returncode == 0 else 12000
        self.last_execution = {
            "argv": argv,
            "returncode": completed.returncode,
            "stdout": stdout[-tail_limit:],
            "stderr": stderr[-tail_limit:],
            "diagnostics": _diagnostics(stdout, stderr),
            "stdout_chars": len(stdout),
            "stderr_chars": len(stderr),
            "output_truncated": len(stdout) > tail_limit or len(stderr) > tail_limit,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        return self.last_execution

    def start(self, argv: list[str], process_id: str) -> dict:
        if not argv:
            raise ValueError("Command is empty")
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
        return {"process_id": process_id, "state": "STOPPED", "returncode": item.process.returncode}

    def logs(self, process_id: str, limit: int = 12000) -> dict:
        item = self._processes.get(process_id)
        if item is None:
            raise LookupError(f"Process not found: {process_id}")
        def tail(path: Path) -> str:
            try:
                data = path.read_bytes()
                return data[-max(512, int(limit)):].decode("utf-8", errors="replace")
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
