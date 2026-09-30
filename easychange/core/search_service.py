from __future__ import annotations

from .workspace import Workspace


class SearchService:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def search(self, query: str, *, case_sensitive: bool = False, limit: int = 100) -> list[dict]:
        results = []
        needle = query if case_sensitive else query.casefold()
        ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".easychange"}
        for path in self.workspace.root_path.rglob("*"):
            if not path.is_file() or any(part in ignored for part in path.parts):
                continue
            try:
                with path.open("r", encoding="utf-8-sig") as stream:
                    for number, line in enumerate(stream, 1):
                        haystack = line if case_sensitive else line.casefold()
                        if needle in haystack:
                            results.append({"id": f"R{len(results) + 1}", "file": path.relative_to(self.workspace.root_path).as_posix(),
                                            "line": number, "text": line.rstrip()})
                            if len(results) >= limit:
                                return results
            except (UnicodeDecodeError, OSError):
                continue
        return results
