from __future__ import annotations

import json
import sys
import time

from easychange.core.command_service import CommandService
from easychange.core.process_service import ProcessService
from easychange.core.runtime_service import ProjectRuntimeService
from easychange.core.workspace import Workspace


def _wait_result(fetch, timeout=10.0):
    deadline = time.time() + timeout
    value = fetch()
    while not value.get("terminal") and time.time() < deadline:
        time.sleep(0.05)
        value = fetch()
    return value


def test_test_job_returns_durable_summary_and_cursor_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    workspace = Workspace.open(workspace_dir)
    processes = ProcessService(workspace.root_path)
    runtime = ProjectRuntimeService(workspace, processes)
    runtime.configure_profile(
        "async-test",
        test_argv=[
            sys.executable,
            "-c",
            "print('A' * 5000); print('3 passed, 1 skipped')",
        ],
        build_argv=[sys.executable, "-c", "print('BUILD OK')"],
    )

    started = runtime.start_test_job("JTEST1", "async-test")
    assert started["state"] == "RUNNING"
    result = _wait_result(lambda: runtime.test_job_result("JTEST1"))

    assert result["terminal"] is True
    assert result["finished"] is True
    assert result["passed"] is True
    assert result["return_code"] == 0
    assert result["tests_passed"] == 3
    assert result["tests_skipped"] == 1
    assert result["tests_failed"] == 0

    cursor = 0
    parts = []
    for _ in range(30):
        page = runtime.runtime_job_logs(
            "JTEST1",
            stream="stdout",
            cursor=cursor,
            max_bytes=512,
        )
        parts.append(page["text"])
        if page["complete"]:
            break
        cursor = page["next_cursor"]
    joined = "".join(parts)
    assert "3 passed, 1 skipped" in joined
    assert len(joined) > 5000
    assert page["complete"] is True
    assert page["remaining_bytes"] == 0


def test_build_job_is_first_class_and_structured(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    workspace = Workspace.open(workspace_dir)
    service = CommandService(workspace)
    try:
        configured = service._execute_structured({
            "op": "operation",
            "type": "runtime_configure",
            "profile_id": "async-build",
            "build_argv": [sys.executable, "-c", "print('BUILD OK')"],
            "test_argv": [sys.executable, "-c", "print('1 passed')"],
        })
        assert configured.ok is True

        started = service._execute_structured({
            "op": "operation",
            "type": "build_job_start",
            "job_id": "JBUILD1",
            "profile": "async-build",
        })
        assert started.ok is True
        assert started.data["results"][0]["data"]["job_id"] == "JBUILD1"

        def fetch():
            result = service._execute_structured({
                "op": "operation",
                "type": "build_job_result",
                "job_id": "JBUILD1",
            })
            assert result.ok is True
            return result.data["results"][0]["data"]

        result = _wait_result(fetch)
        assert result["finished"] is True
        assert result["passed"] is True
        assert result["return_code"] == 0

        logs = service._execute_structured({
            "op": "operation",
            "type": "build_job_logs",
            "job_id": "JBUILD1",
            "stream": "stdout",
            "cursor": 0,
            "max_bytes": 512,
        })
        assert logs.ok is True
        assert "BUILD OK" in logs.data["results"][0]["data"]["text"]
    finally:
        service.close()


def test_generic_job_logs_operation_is_additive(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    service = CommandService(Workspace.open(workspace_dir))
    try:
        started = service._execute_structured({
            "op": "operation",
            "type": "job_start",
            "job_id": "JGEN1",
            "argv": [sys.executable, "-c", "print('GENERIC JOB')"],
        })
        assert started.ok is True

        deadline = time.time() + 10
        while time.time() < deadline:
            status = service._execute_structured({
                "op": "operation", "type": "job_status", "job_id": "JGEN1"
            })
            data = status.data
            if data.get("terminal"):
                break
            time.sleep(0.05)

        logs = service._execute_structured({
            "op": "operation",
            "type": "job_logs",
            "job_id": "JGEN1",
            "stream": "stdout",
            "cursor": 0,
            "max_bytes": 512,
        })
        assert logs.ok is True
        assert "GENERIC JOB" in logs.data["text"]
        assert logs.data["complete"] is True
    finally:
        service.close()
