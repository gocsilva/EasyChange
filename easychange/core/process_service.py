from __future__ import annotations

import subprocess
import time
from pathlib import Path


class ProcessService:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def run(self, argv: list[str], timeout: int = 300) -> dict:
        if not argv:
            raise ValueError("Command is empty")
        started = time.monotonic()
        completed = subprocess.run(argv, cwd=self.cwd, text=True, capture_output=True,
                                   timeout=timeout, check=False, shell=False)
        return {"argv": argv, "returncode": completed.returncode,
                "stdout": completed.stdout[-20000:], "stderr": completed.stderr[-20000:],
                "duration_ms": int((time.monotonic() - started) * 1000)}
