from __future__ import annotations

import subprocess
import time
from pathlib import Path
from dataclasses import dataclass


@dataclass
class RunningProcess:
    process_id: str
    argv: list[str]
    process: subprocess.Popen
    stdout_path: Path
    stderr_path: Path


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
        completed = subprocess.run(argv, cwd=self.cwd, text=True, capture_output=True,
                                   timeout=timeout, check=False, shell=False)
        self.last_execution = {"argv": argv, "returncode": completed.returncode,
                "stdout": completed.stdout[-20000:], "stderr": completed.stderr[-20000:],
                "duration_ms": int((time.monotonic() - started) * 1000)}
        return self.last_execution

    def start(self, argv: list[str], process_id: str) -> dict:
        if not argv: raise ValueError("Command is empty")
        stdout_path, stderr_path = self.root / f"{process_id}.out", self.root / f"{process_id}.err"
        stdout_file, stderr_file = stdout_path.open("wb"), stderr_path.open("wb")
        try:
            proc = subprocess.Popen(argv, cwd=self.cwd, stdin=subprocess.DEVNULL,
                                    stdout=stdout_file, stderr=stderr_file, shell=False)
        finally:
            stdout_file.close(); stderr_file.close()
        self._processes[process_id] = RunningProcess(process_id, argv, proc, stdout_path, stderr_path)
        return {"process_id": process_id, "pid": proc.pid, "state": "RUNNING", "argv": argv}

    def list(self) -> list[dict]:
        output = []
        for key, item in list(self._processes.items()):
            code = item.process.poll()
            output.append({"process_id": key, "pid": item.process.pid,
                           "state": "RUNNING" if code is None else "EXITED",
                           "returncode": code, "argv": item.argv})
        return output

    def stop(self, process_id: str) -> dict:
        item = self._processes.get(process_id)
        if item is None: raise LookupError(f"Process not found: {process_id}")
        if item.process.poll() is None:
            item.process.terminate()
            try: item.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                item.process.kill(); item.process.wait(timeout=3)
        return {"process_id": process_id, "state": "STOPPED", "returncode": item.process.returncode}

    def stop_all(self) -> None:
        for process_id, item in list(self._processes.items()):
            if item.process.poll() is None: self.stop(process_id)
