from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from easychange.core.command_parser import CommandParser
from easychange.core.process_service import ProcessService


def test_command_parser_preserves_launch_profile_spaces_and_windows_paths():
    parser = CommandParser()
    tokens = parser.parse(
        r'run dotnet run --project "C:\Program Files\Acam\Acam.csproj" '
        r'--launch-profile "CronJob - ImportarListasRestritivas"'
    )
    assert tokens == [
        "run",
        "dotnet",
        "run",
        "--project",
        r"C:\Program Files\Acam\Acam.csproj",
        "--launch-profile",
        "CronJob - ImportarListasRestritivas",
    ]


def test_command_parser_supports_escaped_quotes_without_eating_backslashes():
    parser = CommandParser()
    tokens = parser.parse(
        r'run tool.exe --message "hello \"quoted\" world" '
        r'--path "C:\sources\Acam Test\file.txt"'
    )
    assert tokens == [
        "run",
        "tool.exe",
        "--message",
        'hello "quoted" world',
        "--path",
        r"C:\sources\Acam Test\file.txt",
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows command wrapping")
def test_cmd_and_bat_are_wrapped_by_cmd_exe_and_execute(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    cmd = tmp_path / "echo args.cmd"
    cmd.write_text("@echo off\r\necho ARG1=[%~1]\r\necho ARG2=[%~2]\r\n", encoding="utf-8")
    service = ProcessService(tmp_path)

    result = service.run([str(cmd), "one", "two words"], timeout=15)

    assert result["returncode"] == 0
    assert "ARG1=[one]" in result["stdout"]
    assert "ARG2=[two words]" in result["stdout"]
    assert result["requested_argv"] == [str(cmd), "one", "two words"]
    assert result["effective_argv"][1:4] == ["/d", "/s", "/c"]


@pytest.mark.skipif(os.name != "nt", reason="Windows command wrapping")
def test_cmd_slash_c_chain_is_preserved_as_one_argument(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    service = ProcessService(tmp_path)

    result = service.run(
        ["cmd.exe", "/d", "/s", "/c", "echo FIRST&&echo SECOND"],
        timeout=15,
    )

    assert result["returncode"] == 0
    assert "FIRST" in result["stdout"]
    assert "SECOND" in result["stdout"]


def test_startup_reconciles_stale_running_job_to_orphaned(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    first = ProcessService(tmp_path)
    manifest = first.jobs_root / "JORPHAN1.json"
    manifest.write_text(json.dumps({
        "job_id": "JORPHAN1",
        "process_id": "JORPHAN1",
        "pid": 99999999,
        "argv": [sys.executable, "-c", "print('x')"],
        "state": "RUNNING",
        "created_at": time.time() - 3600,
        "started_at": time.time() - 3600,
        "heartbeat_at": time.time() - 3600,
        "finished_at": None,
        "returncode": None,
    }), encoding="utf-8")

    second = ProcessService(tmp_path)
    status = second.job_status("JORPHAN1")

    assert status["state"] == "ORPHANED"
    assert status["terminal"] is True
    assert status["pid_alive"] is False
    assert status["orphan_reason"] == "PROCESS_NOT_ALIVE"


def test_running_job_updates_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    service = ProcessService(tmp_path)
    started = service.job_start(
        [sys.executable, "-c", "import time; time.sleep(0.5)"],
        "JHEART1",
    )
    before = float(started["heartbeat_at"])
    time.sleep(0.05)
    status = service.job_status("JHEART1")
    after = float(status["heartbeat_at"])
    service.job_cancel("JHEART1")

    assert status["state"] == "RUNNING"
    assert after >= before



@pytest.mark.skipif(os.name != "nt", reason="Windows native PID probe")
def test_windows_pid_probe_never_spawns_tasklist(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))

    def forbidden_subprocess(*args, **kwargs):
        raise AssertionError("PID liveness probe must not spawn tasklist/subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden_subprocess)

    assert ProcessService._pid_alive(os.getpid()) is True
    assert ProcessService._pid_alive(99999999) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows native PID probe")
def test_startup_reconcile_many_running_manifests_spawns_zero_probe_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    seed = ProcessService(tmp_path)

    now = time.time()
    for index in range(250):
        job_id = f"JSTALE{index:04d}"
        (seed.jobs_root / f"{job_id}.json").write_text(
            json.dumps({
                "job_id": job_id,
                "process_id": job_id,
                "pid": 90000000 + index,
                "argv": [sys.executable, "-c", "pass"],
                "state": "RUNNING",
                "created_at": now - 3600,
                "started_at": now - 3600,
                "heartbeat_at": now - 3600,
                "finished_at": None,
                "returncode": None,
            }),
            encoding="utf-8",
        )

    calls = {"subprocess": 0}

    def forbidden_subprocess(*args, **kwargs):
        calls["subprocess"] += 1
        raise AssertionError("startup PID reconciliation must not spawn subprocesses")

    monkeypatch.setattr(subprocess, "run", forbidden_subprocess)
    service = ProcessService(tmp_path)

    assert calls["subprocess"] == 0
    orphaned = [
        service.job_status(f"JSTALE{index:04d}")["state"]
        for index in range(250)
    ]
    assert orphaned == ["ORPHANED"] * 250



@pytest.mark.skipif(os.name != "nt", reason="Windows startup reconciliation tuning")
def test_large_startup_reconcile_is_bounded_and_background(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    seed = ProcessService(tmp_path)
    seed.stop_all()

    now = time.time()
    for index in range(300):
        job_id = f"JLAZY{index:04d}"
        (seed.jobs_root / f"{job_id}.json").write_text(
            json.dumps({
                "job_id": job_id,
                "process_id": job_id,
                "pid": 91000000 + index,
                "argv": ["fake.exe"],
                "state": "RUNNING",
                "created_at": now - index,
                "started_at": now - index,
                "heartbeat_at": now - index,
                "finished_at": None,
                "returncode": None,
            }),
            encoding="utf-8",
        )

    started = time.perf_counter()
    service = ProcessService(tmp_path)
    constructor_ms = (time.perf_counter() - started) * 1000.0
    try:
        assert service._startup_reconcile["checked"] <= service.STARTUP_RECONCILE_SYNC_LIMIT
        assert service._reconcile_thread is not None
        assert constructor_ms < 1500.0

        # Correctness is still immediate for any job explicitly queried, even
        # if the background sweep has not reached it yet.
        status = service.job_status("JLAZY0299")
        assert status["state"] == "ORPHANED"
        assert status["pid_alive"] is False
    finally:
        service.stop_all()
