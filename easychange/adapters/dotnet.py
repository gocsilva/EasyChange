from pathlib import Path

from easychange.core.workspace import Workspace


class DotnetAdapter:
    name = "DOTNET"

    def detect(self, workspace: Workspace) -> bool:
        if "DOTNET" in set(workspace.adapters or []):
            return True
        return bool(
            list(workspace.root_path.glob("*.sln"))
            or list(workspace.root_path.glob("*.slnx"))
            or list(workspace.root_path.glob("*.csproj"))
        )

    def describe(self, workspace: Workspace) -> dict:
        return {"name": self.name, "target": self._target(workspace)}

    @staticmethod
    def _target(workspace: Workspace) -> str | None:
        """Choose a deterministic nested .NET build/test target when unambiguous."""
        candidates = []
        for raw in workspace.projects or []:
            value = str(raw or "").replace("\\", "/").strip()
            suffix = Path(value).suffix.casefold()
            if suffix in {".sln", ".slnx", ".csproj"}:
                candidates.append(value)
        candidates = sorted(
            dict.fromkeys(candidates),
            key=lambda value: (value.count("/"), value.casefold()),
        )
        solutions = [
            value for value in candidates
            if Path(value).suffix.casefold() in {".sln", ".slnx"}
        ]
        if len(solutions) == 1:
            return solutions[0]
        if len(candidates) == 1:
            return candidates[0]
        # Multiple solutions/projects are intentionally not guessed. A custom
        # runtime profile remains the explicit way to choose among ambiguity.
        return None

    @staticmethod
    def _has_restore_assets(workspace: Workspace) -> bool:
        # project.assets.json is NuGet's restore output. Avoid a restore round
        # trip on every build/test once the workspace is already restored.
        try:
            return next(workspace.root_path.rglob("project.assets.json"), None) is not None
        except OSError:
            return False

    def build(self, workspace: Workspace) -> list[str] | None:
        argv = ["dotnet", "build"]
        target = self._target(workspace)
        if target:
            argv.append(target)
        if self._has_restore_assets(workspace):
            argv.append("--no-restore")
        return argv

    def test(self, workspace: Workspace) -> list[str] | None:
        argv = ["dotnet", "test"]
        target = self._target(workspace)
        if target:
            argv.append(target)
        if self._has_restore_assets(workspace):
            argv.append("--no-restore")
        return argv
