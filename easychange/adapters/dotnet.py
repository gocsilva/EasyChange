from easychange.core.workspace import Workspace


class DotnetAdapter:
    name = "DOTNET"

    def detect(self, workspace: Workspace) -> bool:
        return bool(list(workspace.root_path.glob("*.sln")) or list(workspace.root_path.glob("*.csproj")))

    def describe(self, workspace: Workspace) -> dict:
        return {"name": self.name}

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
        if self._has_restore_assets(workspace):
            argv.append("--no-restore")
        return argv

    def test(self, workspace: Workspace) -> list[str] | None:
        argv = ["dotnet", "test"]
        if self._has_restore_assets(workspace):
            argv.append("--no-restore")
        return argv
