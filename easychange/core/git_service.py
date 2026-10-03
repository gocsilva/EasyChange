from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

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

    @staticmethod
    def _safe_ref(value: str | None) -> str | None:
        if value is None:
            return None
        ref = str(value).strip()
        if not ref:
            return None
        if len(ref) > 256 or ref.startswith("-") or any(ch.isspace() or ord(ch) < 32 for ch in ref):
            raise ValueError("INVALID_GIT_REF")
        return ref

    def _diff_argv(
        self,
        *,
        base_ref: str | None,
        head_ref: str | None,
        path: str | None,
        name_only: bool,
        name_status: bool,
        unified_lines: int,
    ) -> list[str]:
        if name_only and name_status:
            raise ValueError("name_only and name_status are mutually exclusive")
        base = self._safe_ref(base_ref)
        head = self._safe_ref(head_ref)
        argv = ["git", "diff", "--no-ext-diff"]
        if name_status:
            argv.append("--name-status")
        elif name_only:
            argv.append("--name-only")
        else:
            argv.append(f"--unified={max(0, min(100, int(unified_lines)))}")
        if base and head:
            argv.append(f"{base}...{head}")
        elif base:
            argv.append(base)
        elif head:
            argv.append(head)
        if path:
            argv.extend(["--", str(path)])
        return argv

    @staticmethod
    def _parse_name_status_line(line: str) -> dict:
        parts = line.split("	")
        status = parts[0] if parts else ""
        if len(parts) >= 3 and status[:1] in {"R", "C"}:
            return {
                "status": status,
                "old_path": parts[1],
                "path": parts[2],
            }
        return {
            "status": status,
            "path": parts[1] if len(parts) > 1 else "",
        }

    @staticmethod
    def _utf8_page(payload: bytes, cursor: int, max_bytes: int) -> tuple[str, int]:
        start = max(0, min(len(payload), int(cursor)))
        end = min(len(payload), start + max(256, min(2 * 1024 * 1024, int(max_bytes))))
        if end < len(payload):
            while end > start:
                try:
                    text = payload[start:end].decode("utf-8")
                    return text, end
                except UnicodeDecodeError as exc:
                    if exc.start <= 0:
                        end -= 1
                    else:
                        end = start + exc.start
        return payload[start:end].decode("utf-8", errors="replace"), end

    def diff_refs(
        self,
        *,
        base_ref: str | None = None,
        head_ref: str | None = None,
        path: str | None = None,
        name_only: bool = False,
        name_status: bool = False,
        unified_lines: int = 3,
        cursor: int = 0,
        max_bytes: int = 65536,
        max_items: int = 500,
    ) -> dict:
        """Structured/paginated Git diff; file lists are never silently truncated."""
        argv = self._diff_argv(
            base_ref=base_ref,
            head_ref=head_ref,
            path=path,
            name_only=bool(name_only),
            name_status=bool(name_status),
            unified_lines=unified_lines,
        )
        completed = subprocess.run(
            argv,
            cwd=self.workspace.root_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
        )
        if completed.returncode:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(error or "Git diff failed")

        if name_only or name_status:
            lines = completed.stdout.decode("utf-8", errors="replace").splitlines()
            start = max(0, min(len(lines), int(cursor)))
            limit = max(1, min(5000, int(max_items)))
            selected = lines[start:start + limit]
            if name_status:
                items = [self._parse_name_status_line(line) for line in selected if line]
            else:
                items = [{"path": line} for line in selected if line]
            next_cursor = start + len(selected)
            complete = next_cursor >= len(lines)
            return {
                "mode": "name_status" if name_status else "name_only",
                "base_ref": base_ref,
                "head_ref": head_ref,
                "path": path,
                "items": items,
                "cursor": start,
                "next_cursor": None if complete else next_cursor,
                "total_count": len(lines),
                "remaining_count": max(0, len(lines) - next_cursor),
                "complete": complete,
            }

        start = max(0, min(len(completed.stdout), int(cursor)))
        text, next_cursor = self._utf8_page(completed.stdout, start, max_bytes)
        complete = next_cursor >= len(completed.stdout)
        return {
            "mode": "patch",
            "base_ref": base_ref,
            "head_ref": head_ref,
            "path": path,
            "unified_lines": max(0, min(100, int(unified_lines))),
            "text": text,
            "cursor": start,
            "next_cursor": None if complete else next_cursor,
            "total_bytes": len(completed.stdout),
            "remaining_bytes": max(0, len(completed.stdout) - next_cursor),
            "complete": complete,
        }

    @staticmethod
    def _safe_ref(value: str, *, default: str = "HEAD") -> str:
        ref = str(value or default).strip()
        if not ref:
            ref = default
        if ref.startswith("-") or any(ch in ref for ch in ("\0", "\r", "\n")) or len(ref) > 240:
            raise ValueError("INVALID_GIT_REF")
        return ref

    def _diff_range(self, base_ref: str, head_ref: str) -> str:
        base = self._safe_ref(base_ref)
        head = self._safe_ref(head_ref or "HEAD")
        return f"{base}...{head}"

    def diff_refs(
        self,
        *,
        base_ref: str,
        head_ref: str = "HEAD",
        path: str | None = None,
        name_only: bool = False,
        name_status: bool = False,
        unified_lines: int = 3,
        cursor: int = 0,
        max_bytes: int = 65536,
        max_items: int = 500,
    ) -> dict:
        """Compare refs with deterministic pagination.

        File-list modes page by item and never silently truncate. Patch mode
        pages directly from a temporary file so huge PR diffs are not buffered
        in RAM.
        """
        if name_only and name_status:
            raise ValueError("GIT_DIFF_MODE_CONFLICT")
        cursor = max(0, int(cursor or 0))
        diff_range = self._diff_range(base_ref, head_ref)
        common = ["diff", "--no-ext-diff"]
        if name_status:
            output = self._run(*common, "--name-status", diff_range, "--", *([path] if path else []))
            items = []
            for line in output.splitlines():
                parts = line.split("	")
                if not parts:
                    continue
                status = parts[0]
                if status.startswith(("R", "C")) and len(parts) >= 3:
                    items.append({
                        "status": status,
                        "old_path": parts[1],
                        "path": parts[2],
                    })
                elif len(parts) >= 2:
                    items.append({"status": status, "path": parts[-1]})
            limit = max(1, min(5000, int(max_items or 500)))
            selected = items[cursor:cursor + limit]
            next_cursor = cursor + len(selected)
            complete = next_cursor >= len(items)
            return {
                "mode": "name_status",
                "base_ref": base_ref,
                "head_ref": head_ref or "HEAD",
                "range": diff_range,
                "path": path,
                "items": selected,
                "total_count": len(items),
                "returned": len(selected),
                "cursor": cursor,
                "next_cursor": None if complete else next_cursor,
                "remaining_count": max(0, len(items) - next_cursor),
                "complete": complete,
            }
        if name_only:
            output = self._run(*common, "--name-only", diff_range, "--", *([path] if path else []))
            items = [{"path": line} for line in output.splitlines() if line]
            limit = max(1, min(5000, int(max_items or 500)))
            selected = items[cursor:cursor + limit]
            next_cursor = cursor + len(selected)
            complete = next_cursor >= len(items)
            return {
                "mode": "name_only",
                "base_ref": base_ref,
                "head_ref": head_ref or "HEAD",
                "range": diff_range,
                "path": path,
                "items": selected,
                "total_count": len(items),
                "returned": len(selected),
                "cursor": cursor,
                "next_cursor": None if complete else next_cursor,
                "remaining_count": max(0, len(items) - next_cursor),
                "complete": complete,
            }

        unified = max(0, min(200, int(unified_lines or 0)))
        page_bytes = max(256, min(1024 * 1024, int(max_bytes or 65536)))
        args = ["git", *common, f"--unified={unified}", diff_range, "--"]
        if path:
            args.append(path)

        with tempfile.TemporaryFile(mode="w+b") as stdout_file:
            completed = subprocess.run(
                args,
                cwd=self.workspace.root_path,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
            if completed.returncode:
                stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
                raise RuntimeError(stderr.strip() or "Git diff failed")
            total_bytes = stdout_file.seek(0, 2)
            start = min(cursor, total_bytes)
            stdout_file.seek(start)
            raw = stdout_file.read(min(page_bytes + 4096, max(0, total_bytes - start)))

        if len(raw) > page_bytes:
            boundary = raw.rfind(b"\n", 0, page_bytes + 1)
            cut = boundary + 1 if boundary >= 0 else page_bytes
        else:
            cut = len(raw)
        chunk = raw[:cut]
        next_cursor = start + len(chunk)
        complete = next_cursor >= total_bytes
        return {
            "mode": "patch",
            "base_ref": base_ref,
            "head_ref": head_ref or "HEAD",
            "range": diff_range,
            "path": path,
            "unified_lines": unified,
            "text": chunk.decode("utf-8", errors="replace"),
            "cursor": start,
            "next_cursor": None if complete else next_cursor,
            "returned_bytes": len(chunk),
            "total_bytes": total_bytes,
            "remaining_bytes": max(0, total_bytes - next_cursor),
            "complete": complete,
        }

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
