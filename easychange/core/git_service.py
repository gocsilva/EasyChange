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

    def diff(self) -> str:
        return self._run("diff", "--no-ext-diff", "--")
