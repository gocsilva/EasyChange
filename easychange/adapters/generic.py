from easychange.core.workspace import Workspace


class GenericAdapter:
    name = "GENERIC"
    def detect(self, workspace: Workspace) -> bool: return True
    def describe(self, workspace: Workspace) -> dict: return {"name": self.name}
    def build(self, workspace: Workspace) -> list[str] | None: return None
    def test(self, workspace: Workspace) -> list[str] | None: return None
