from __future__ import annotations

import subprocess

from .workspace import Workspace


class GitService:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _run(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.workspace.root_path, text=True,
                                capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or "Git command failed")
        return result.stdout.strip()

    def status(self) -> dict:
        branch = self._run("branch", "--show-current")
        lines = self._run("status", "--short").splitlines()
        return {"branch": branch, "changes": lines, "clean": not lines}

    def diff(self, path: str | None = None) -> str:
        return self._run("diff", "--no-ext-diff", "--", *( [path] if path else [] ))

    def branch(self) -> dict:
        return {"current": self._run("branch", "--show-current"),
                "branches": self._run("branch", "--list").splitlines()}

    def log(self, limit: int = 10) -> list[dict]:
        output = self._run("log", f"-{max(1, min(limit, 100))}", "--date=iso-strict",
                           "--pretty=format:%h%x1f%ad%x1f%s")
        return [dict(zip(("hash", "date", "subject"), line.split("\x1f", 2))) for line in output.splitlines()]
