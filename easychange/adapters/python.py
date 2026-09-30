from pathlib import Path
import sys
from easychange.core.workspace import Workspace


class PythonAdapter:
    name = "PYTHON"
    def detect(self, workspace: Workspace) -> bool:
        return any((workspace.root_path / name).exists() for name in ("pyproject.toml", "requirements.txt", "setup.py"))
    def describe(self, workspace: Workspace) -> dict: return {"name": self.name}
    def build(self, workspace: Workspace) -> list[str] | None: return None
    def test(self, workspace: Workspace) -> list[str] | None:
        if (workspace.root_path / "pytest.ini").exists() or (workspace.root_path / "tests").is_dir():
            candidates = [workspace.root_path / ".venv" / "Scripts" / "python.exe",
                          workspace.root_path / ".venv" / "bin" / "python"]
            interpreter = next((str(path) for path in candidates if path.exists()), sys.executable)
            return [interpreter, "-m", "pytest"]
        return None
