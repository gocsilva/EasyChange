from easychange.core.workspace import Workspace


class NodeAdapter:
    name = "NODE"
    def detect(self, workspace: Workspace) -> bool: return (workspace.root_path / "package.json").exists()
    def describe(self, workspace: Workspace) -> dict: return {"name": self.name}
    def build(self, workspace: Workspace) -> list[str] | None: return ["npm", "run", "build", "--if-present"]
    def test(self, workspace: Workspace) -> list[str] | None: return ["npm", "test", "--", "--run"]
