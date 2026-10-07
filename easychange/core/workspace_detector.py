from __future__ import annotations

import os
from pathlib import Path


MARKERS: tuple[tuple[str, str], ...] = (
    ("*.sln", "DOTNET"), ("*.slnx", "DOTNET"), ("*.csproj", "DOTNET"),
    ("pyproject.toml", "PYTHON"), ("requirements.txt", "PYTHON"),
    ("package.json", "NODE"), ("Cargo.toml", "RUST"), ("go.mod", "GO"),
    ("pom.xml", "JAVA"), ("build.gradle", "JAVA"), ("CMakeLists.txt", "CMAKE"),
    ("*.uproject", "UNREAL"), ("project.godot", "GODOT"),
)

_SKIP_DIRS = {
    ".git", ".easychange", "bin", "obj", "node_modules", ".venv", "venv",
    "dist", "build", "packages", "TestResults", ".vs",
}
_MAX_MARKER_DEPTH = 5


def _matches(name: str, pattern: str) -> bool:
    if pattern.startswith("*."):
        return name.casefold().endswith(pattern[1:].casefold())
    return name.casefold() == pattern.casefold()


def _project_markers(root: Path) -> list[tuple[str, str]]:
    """Discover project markers in shallow repository subfolders.

    EasyChange is often opened at a Git/repository root while the .sln/.csproj
    lives one or more directories below it. The old root.glob-only detector
    classified those repositories as plain GIT, so :build/:test/:validate
    reported that no .NET profile existed. A bounded walk fixes that without
    reintroducing the expensive full-tree scans that previously hurt startup.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        iterator = os.walk(root)
    except OSError:
        return found

    for current_raw, dirs, files in iterator:
        current = Path(current_raw)
        try:
            relative_dir = current.relative_to(root)
        except ValueError:
            continue
        depth = len(relative_dir.parts)
        dirs[:] = [
            value
            for value in dirs
            if value not in _SKIP_DIRS
            and not value.startswith(".")
        ]
        if depth >= _MAX_MARKER_DEPTH:
            dirs[:] = []

        for file_name in files:
            for pattern, kind in MARKERS:
                if not _matches(file_name, pattern):
                    continue
                path = current / file_name
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    continue
                key = relative.casefold()
                if key in seen:
                    break
                seen.add(key)
                found.append((relative, kind))
                break

    found.sort(key=lambda item: (item[0].count("/"), item[0].casefold()))
    return found


def detect_workspace(root: Path) -> tuple[str, list[str], list[str]]:
    adapters: list[str] = []
    projects: list[str] = []
    for relative, kind in _project_markers(root):
        projects.append(relative)
        if kind not in adapters:
            adapters.append(kind)

    git_present = any((parent / ".git").exists() for parent in (root, *root.parents))
    if git_present and "GIT" not in adapters:
        adapters.insert(0, "GIT")

    # GIT is a source-control capability, not the project ecosystem. Prefer a
    # detected project adapter for workspace.kind so downstream build/test logic
    # does not lose the .NET/Node/Python identity merely because the repo is Git.
    project_adapters = [value for value in adapters if value != "GIT"]
    if project_adapters:
        kind = project_adapters[0]
    elif git_present:
        kind = "GIT"
    else:
        kind = "GENERIC"

    if not adapters:
        adapters = ["GENERIC"]
    return kind, adapters, projects
