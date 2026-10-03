from __future__ import annotations

import sys
import time

from easychange.core.process_service import ProcessService
from easychange.core.runtime_service import ProjectRuntimeService
from easychange.core.workspace import Workspace


def test_build_job_completes_without_visual_polling_and_same_job_is_recovered(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    workspace = Workspace.open(workspace_dir)
    processes = ProcessService(workspace.root_path)
    runtime = ProjectRuntimeService(workspace, processes)
    runtime.configure_profile(
        "visual-gap-build",
        build_argv=[
            sys.executable,
            "-c",
            "import time; time.sleep(0.15); print('BUILD-GAP-OK')",
        ],
        test_argv=[sys.executable, "-c", "print('1 passed')"],
    )

    started = runtime.start_build_job("JHDMIGAP1", "visual-gap-build")
    assert started["job_id"] == "JHDMIGAP1"
    assert started["state"] == "RUNNING"

    # Simulate HDMI/caller silence: the job receives no status/visual polls.
    time.sleep(0.30)

    result = runtime.build_job_result("JHDMIGAP1")
    assert result["job_id"] == "JHDMIGAP1"
    assert result["finished"] is True
    assert result["passed"] is True
    assert result["return_code"] == 0

    logs = runtime.runtime_job_logs(
        "JHDMIGAP1", stream="stdout", cursor=0, max_bytes=4096
    )
    assert "BUILD-GAP-OK" in logs["text"]
    assert logs["complete"] is True
