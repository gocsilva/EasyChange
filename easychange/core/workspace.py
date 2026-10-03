from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass(slots=True)
class Workspace:
    root_path: Path
    name: str
    kind: str = "GENERIC"
    projects: list[str] = field(default_factory=list)
    adapters: list[str] = field(default_factory=list)

    @classmethod
    def open(cls, path: str | Path) -> "Workspace":
        root = Path(path).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        from .workspace_detector import detect_workspace
        kind, adapters, projects = detect_workspace(root)
        workspace = cls(root, root.name or str(root), kind, projects, adapters)
        workspace.ensure_easychange_git_excluded()
        return workspace

    def _git_dir(self) -> Path | None:
        marker = self.root_path / ".git"
        if marker.is_dir():
            return marker
        if marker.is_file():
            try:
                first = marker.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
            except (OSError, IndexError):
                return None
            if first.casefold().startswith("gitdir:"):
                raw = first.split(":", 1)[1].strip()
                candidate = Path(raw)
                if not candidate.is_absolute():
                    candidate = (self.root_path / candidate).resolve(strict=False)
                return candidate
        return None

    def ensure_easychange_git_excluded(self) -> dict:
        """Keep EasyChange runtime artifacts out of Git status without touching .gitignore."""
        git_dir = self._git_dir()
        if git_dir is None:
            return {"git": False, "excluded": False}
        exclude = git_dir / "info" / "exclude"
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            raw = exclude.read_bytes() if exclude.exists() else b""
            current = raw.decode("utf-8", errors="replace")
            normalized = {line.strip() for line in current.splitlines()}
            if "/.easychange/" in normalized:
                return {"git": True, "excluded": True, "changed": False}
            prefix = b"" if not raw or raw.endswith(b"\n") else b"\n"
            with exclude.open("ab") as handle:
                handle.write(prefix + b"/.easychange/\n")
                handle.flush()
                os.fsync(handle.fileno())
            return {"git": True, "excluded": True, "changed": True}
        except OSError as exc:
            return {
                "git": True,
                "excluded": False,
                "error": type(exc).__name__,
            }

    def resolve(self, path: str | Path, *, must_exist: bool = False) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root_path / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root_path)
        except ValueError as exc:
            raise PermissionError(f"Path escapes workspace: {path}") from exc
        if must_exist and not resolved.exists():
            raise FileNotFoundError(str(resolved))
        return resolved
