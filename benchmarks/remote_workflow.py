"""Simulate the reduced-round-trip HDMI/ESP keyboard workflow."""
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
        commands = [":locate NumeroProtocolo --context 2"]
        actions = []
        try:
            located = service.execute(commands[-1]); actions.append(located)
            result_id = located.data["matches"][0]["id"]
            commands.append(f':edit-result {result_id} "NumeroProtocolo = 8"')
            actions.append(service.execute(commands[-1]))
            commands.append(":diff && :test")
            actions.append(service.execute(commands[-1]))
            commands.append(":undo")
            actions.append(service.execute(commands[-1]))
            failures = [result for result in actions if not result.ok]
            compact = [result.render("compact") for result in actions]
            keypresses = sum(len(command) + 1 for command in commands)
            print(f"REMOTE_WORKFLOW_COMMANDS={len(actions)}")
            print(f"REMOTE_WORKFLOW_PASSED={len(actions) - len(failures)}/{len(actions)}")
            print(f"REMOTE_WORKFLOW_APPROX_KEYPRESSES={keypresses}")
            print(f"REMOTE_WORKFLOW_COMPACT_RESULTS_WITH_DATA={sum('ms {' in value for value in compact)}/{len(compact)}")
            print(f"REMOTE_WORKFLOW_DURATION_MS={sum(result.duration_ms for result in actions)}")
            return 1 if failures else 0
        finally:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
