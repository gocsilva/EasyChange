from __future__ import annotations

import os
import stat

from .workspace import Workspace


class SearchService:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def search(self, query: str, *, case_sensitive: bool = False, limit: int = 100) -> list[dict]:
        if not isinstance(query, str):
            raise ValueError("INVALID_SEARCH_QUERY")
        if not query or type(limit) is not int or limit <= 0:
            return []
        results = []
        needle = query if case_sensitive else query.casefold()
        ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".easychange"}
        for path in self.workspace.root_path.rglob("*"):
            # Windows path matching is case-insensitive: .GIT, .VENV or
            # .EASYCHANGE must never bypass ignored metadata/runtime dirs.
            if any(part.casefold() in ignored for part in path.parts):
                continue
            descriptor = -1
            try:
                named = path.lstat()
                # Search is an ordinary workspace read, not a path to
                # external files through symlinks, NTFS hardlinks or devices.
                if not stat.S_ISREG(named.st_mode) or named.st_nlink != 1:
                    continue
                path.resolve(strict=True).relative_to(self.workspace.root_path)
                flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
                descriptor = os.open(path, flags)
                opened = os.fstat(descriptor)
                current = path.lstat()
                if (not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or not stat.S_ISREG(current.st_mode)
                        or current.st_nlink != 1
                        or (opened.st_dev, opened.st_ino) !=
                           (current.st_dev, current.st_ino)):
                    continue
                with os.fdopen(descriptor, "r", encoding="utf-8-sig") as stream:
                    descriptor = -1
                    for number, line in enumerate(stream, 1):
                        haystack = line if case_sensitive else line.casefold()
                        if needle in haystack:
                            results.append({"id": f"R{len(results) + 1}", "file": path.relative_to(self.workspace.root_path).as_posix(),
                                            "line": number, "text": line.rstrip()})
                            if len(results) >= limit:
                                return results
            except (UnicodeDecodeError, OSError, ValueError):
                continue
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        return results
