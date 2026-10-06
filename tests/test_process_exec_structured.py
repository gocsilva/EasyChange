from __future__ import annotations

import sys
import time

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def _primary(result):
    return result.data["results"][0]["data"] if "results" in result.data else result.data


def test_process_exec_preserves_structured_argv_and_finishes_fast(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    service = CommandService(Workspace.open(tmp_path))
    try:
        value = "CronJob - ImportarListasRestritivas"
        result = service._execute_structured({
            "op": "operation",
            "type": "process_exec",
            "argv": [
                sys.executable,
                "-c",
                "import sys; print(sys.argv[1])",
                value,
            ],
            "wait_ms": 3000,
        })
        assert result.ok is True
        data = _primary(result)
        assert data["execution"]["state"] == "FINISHED"
        assert data["execution"]["success"] is True
        assert data["execution"]["return_code"] == 0
        assert value in data["stdout"]
        assert data["argv"][-1] == value
        assert data["result"]["available"] is True
        assert data["result"]["paged_logs"] is True
    finally:
        service.close()


def test_process_exec_returns_running_job_instead_of_blocking(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    service = CommandService(Workspace.open(tmp_path))
    try:
        started = time.monotonic()
        result = service._execute_structured({
            "op": "operation",
            "type": "process_exec",
            "argv": [
                sys.executable,
                "-c",
                "import time; time.sleep(1.0); print('done')",
            ],
            "wait_ms": 50,
        })
        elapsed = time.monotonic() - started
        assert result.ok is True
        data = _primary(result)
        assert elapsed < 0.7
        assert data["execution"]["state"] == "RUNNING"
        assert data["execution"]["success"] is None
        assert data["job_id"]
        assert data["result"]["available"] is False

        deadline = time.time() + 5
        while time.time() < deadline:
            final = service.processes.job_result(data["job_id"])
            if final.get("terminal"):
                break
            time.sleep(0.03)
        assert final["state"] == "SUCCEEDED"
        assert "done" in final["stdout"]
    finally:
        service.close()


def test_process_exec_command_failure_is_separate_from_protocol_success(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    service = CommandService(Workspace.open(tmp_path))
    try:
        result = service._execute_structured({
            "op": "operation",
            "type": "process_exec",
            "argv": [sys.executable, "-c", "import sys; print('bad'); sys.exit(7)"],
            "wait_ms": 3000,
        })
        assert result.ok is True
        data = _primary(result)
        assert data["transport"]["execution_channel"] == "healthy"
        assert data["execution"]["state"] == "FINISHED"
        assert data["execution"]["success"] is False
        assert data["execution"]["return_code"] == 7
        assert data["result"]["available"] is True
    finally:
        service.close()


def test_process_exec_is_not_continue_on_error_safe(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        caps = service.execute(":capabilities").data["capabilities"]["ai_machine"]["structured_operations"]
        assert caps["operations"]["process_exec"]["family"] == "runtime"
        assert caps["operations"]["process_exec"]["continue_on_error_safe"] is False
    finally:
        service.close()
