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

    def status(self, summary: bool = False) -> dict:
        """Return branch + short changes with one Git process."""
        output = self._run("status", "--short", "--branch")
        lines = output.splitlines()
        header = lines[0] if lines and lines[0].startswith("## ") else ""
        changes = lines[1:] if header else lines
        branch = ""
        branch_text = ""
        if header:
            branch_text = header[3:].strip()
            if branch_text.startswith("HEAD "):
                branch = "HEAD"
            else:
                branch = branch_text.split("...", 1)[0].split(" ", 1)[0]
        result = {"branch": branch, "changes": changes, "clean": not changes}
        if summary:
            import re
            ahead_match = re.search(r"ahead (\d+)", branch_text)
            behind_match = re.search(r"behind (\d+)", branch_text)
            result.update({
                "change_count": len(changes),
                "ahead": int(ahead_match.group(1)) if ahead_match else 0,
                "behind": int(behind_match.group(1)) if behind_match else 0,
            })
        return result

    def diff(self, path: str | None = None) -> str:
        return self._run("diff", "--no-ext-diff", "--", *( [path] if path else [] ))

    def branch(self, summary: bool = False) -> dict:
        branches = self._run("branch", "--list").splitlines()
        result = {
            "current": self._run("branch", "--show-current"),
            "branches": branches,
        }
        if summary:
            result["count"] = len(branches)
        return result

    def log(self, limit: int = 10) -> list[dict]:
        output = self._run("log", f"-{max(1, min(limit, 100))}", "--date=iso-strict",
                           "--pretty=format:%h%x1f%ad%x1f%s")
        return [dict(zip(("hash", "date", "subject"), line.split("\x1f", 2))) for line in output.splitlines()]
