from __future__ import annotations

import os
import sys
import time

import pytest

from easychange.core.process_service import ProcessService


def test_structured_argv_preserves_argument_with_spaces(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)

    value = "CronJob - ImportarListasRestritivas"
    result = service.run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", value],
        timeout=30,
    )

    assert result["returncode"] == 0
    assert value in result["stdout"]
    assert result["argv"][-1] == value
    assert result["exec_argv"][-1] == value


@pytest.mark.skipif(os.name != "nt", reason="Windows .cmd semantics")
@pytest.mark.parametrize("suffix", [".cmd", ".bat"])
def test_cmd_and_bat_are_wrapped_by_cmd_and_execute_with_quoted_args(
    tmp_path, monkeypatch, suffix
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script_dir = workspace / "folder with spaces"
    script_dir.mkdir()
    script = script_dir / f"echo-args{suffix}"
    script.write_text(
        "@echo off\r\n"
        "setlocal EnableDelayedExpansion\r\n"
        "set \"arg1=%~1\"\r\n"
        "set \"arg2=%~2\"\r\n"
        "echo FIRST=[!arg1!]\r\n"
        "echo SECOND=[!arg2!]\r\n",
        encoding="utf-8",
    )
    service = ProcessService(workspace)

    result = service.run(
        [str(script), "Name With Spaces", "A&B"],
        timeout=30,
    )

    assert result["returncode"] == 0
    assert "FIRST=[Name With Spaces]" in result["stdout"]
    assert "SECOND=[A&B]" in result["stdout"]
    assert result["argv"][0] == str(script)
    assert result["exec_argv"][1:4] == ["/d", "/s", "/c"]


def test_dead_running_manifest_becomes_orphaned_on_restart_and_does_not_block_new_job(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = ProcessService(workspace)
    first._write_job("JSTALE01", {
        "job_id": "JSTALE01",
        "process_id": "JSTALE01",
        "pid": 2147483000,
        "argv": [sys.executable, "-c", "print('old')"],
        "exec_argv": [sys.executable, "-c", "print('old')"],
        "state": "RUNNING",
        "created_at": time.time() - 60,
        "started_at": time.time() - 60,
        "heartbeat_at": time.time() - 60,
        "finished_at": None,
        "returncode": None,
        "metadata": {},
        "terminal": False,
    })

    second = ProcessService(workspace)
    stale = second.job_status("JSTALE01")
    assert stale["state"] == "ORPHANED"
    assert stale["terminal"] is True
    assert stale["orphan_reason"] in {
        "PROCESS_NOT_ALIVE",
        "PID_NOT_ALIVE_AT_STARTUP",
        "RUNNING_PID_NOT_ALIVE",
    }

    started = second.job_start(
        [sys.executable, "-c", "print('new-job-ok')"],
        "JNEW01",
    )
    assert started["state"] == "RUNNING"
    deadline = time.time() + 10
    while time.time() < deadline:
        result = second.job_result("JNEW01")
        if result.get("terminal"):
            break
        time.sleep(0.02)
    assert result["state"] == "SUCCEEDED"
    assert "new-job-ok" in result["stdout"]


def test_job_heartbeat_updates_while_process_is_alive(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)
    service.JOB_HEARTBEAT_INTERVAL_SECONDS = 0

    started = service.job_start(
        [sys.executable, "-c", "import time; time.sleep(0.5)"],
        "JHEART01",
    )
    old = started["heartbeat_at"]
    time.sleep(0.03)
    status = service.job_status("JHEART01")
    assert status["state"] == "RUNNING"
    assert status["heartbeat_at"] >= old
    assert status["attached"] is True

    service.job_cancel("JHEART01")


def test_job_cancel_is_terminal_and_idempotent_for_attached_job(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)

    service.job_start(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        "JCANCEL01",
    )
    cancelled = service.job_cancel("JCANCEL01")
    assert cancelled["state"] == "CANCELLED"
    assert cancelled["terminal"] is True

    again = service.job_status("JCANCEL01")
    assert again["state"] == "CANCELLED"
    assert again["terminal"] is True
