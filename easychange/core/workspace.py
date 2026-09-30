from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


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
        return cls(root, root.name or str(root), kind, projects, adapters)

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
