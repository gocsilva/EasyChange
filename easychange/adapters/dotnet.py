from easychange.core.workspace import Workspace


class DotnetAdapter:
    name = "DOTNET"
    def detect(self, workspace: Workspace) -> bool:
        return bool(list(workspace.root_path.glob("*.sln")) or list(workspace.root_path.glob("*.csproj")))
    def describe(self, workspace: Workspace) -> dict: return {"name": self.name}
    def build(self, workspace: Workspace) -> list[str] | None: return ["dotnet", "build"]
    def test(self, workspace: Workspace) -> list[str] | None: return ["dotnet", "test"]
