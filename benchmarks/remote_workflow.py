"""Simulate the concise keyboard-only search/edit/test/undo workflow."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="easychange-remote-") as folder:
        root = Path(folder)
        (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.0.1'\n", encoding="utf-8")
        (root / "src").mkdir(); (root / "tests").mkdir()
        source = root / "src" / "sample.py"
        source.write_text("def get_protocol():\n    return 'old'\n\nNumeroProtocolo = 7\n", encoding="utf-8")
        (root / "tests" / "test_sample.py").write_text("def test_protocol():\n    assert True\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "src/sample.py"], cwd=root, check=True)
        service = CommandService(Workspace.open(root))
        actions = []
        search = service.execute(":search NumeroProtocolo"); actions.append(search)
        result_id = search.data["matches"][0]["id"]
        actions.append(service.execute(f":context {result_id}"))
        actions.append(service.execute(':replace-line src/sample.py 2 "    return \'new\'"'))
        actions.append(service.execute(":diff"))
        actions.append(service.execute(":test"))
        actions.append(service.execute(":undo"))
        failures = [result for result in actions if not result.ok or (result.command == "test" and result.data.get("returncode") != 0)]
        print(f"REMOTE_WORKFLOW_COMMANDS={len(actions)}")
        print(f"REMOTE_WORKFLOW_PASSED={len(actions) - len(failures)}/{len(actions)}")
        print(f"REMOTE_WORKFLOW_DURATION_MS={sum(result.duration_ms for result in actions)}")
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
