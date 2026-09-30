from pathlib import Path


MARKERS: tuple[tuple[str, str], ...] = (
    ("*.sln", "DOTNET"), ("*.csproj", "DOTNET"),
    ("pyproject.toml", "PYTHON"), ("requirements.txt", "PYTHON"),
    ("package.json", "NODE"), ("Cargo.toml", "RUST"), ("go.mod", "GO"),
    ("pom.xml", "JAVA"), ("build.gradle", "JAVA"), ("CMakeLists.txt", "CMAKE"),
    ("*.uproject", "UNREAL"), ("project.godot", "GODOT"),
)


def detect_workspace(root: Path) -> tuple[str, list[str], list[str]]:
    adapters = []
    projects = []
    for pattern, kind in MARKERS:
        for item in root.glob(pattern):
            if item.is_file():
                projects.append(item.name)
                if kind not in adapters:
                    adapters.append(kind)
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        adapters.insert(0, "GIT")
    kind = adapters[-1] if len(adapters) == 1 else ("GIT" if "GIT" in adapters else (adapters[0] if adapters else "GENERIC"))
    if not adapters:
        adapters = ["GENERIC"]
    return kind, adapters, projects
