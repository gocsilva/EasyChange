from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from easychange.core.process_service import (
    ProcessService,
    RunningProcess,
    _durable_job_start_lock,
)


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


def test_failed_running_manifest_write_stops_new_child_before_raising(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)

    class Child:
        pid = 99991

        def __init__(self):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            assert timeout is not None
            return self.returncode

        def kill(self):
            self.returncode = -9

    child = Child()
    original_write_job = service._write_job

    def fail_running_manifest(job_id, data, *, durable=True):
        if data.get("state") == "RUNNING":
            raise OSError("MANIFEST_DISK_FULL")
        return original_write_job(job_id, data, durable=durable)

    def fake_start(argv, process_id, **_kwargs):
        stdout = service.root / f"{process_id}.out"
        stderr = service.root / f"{process_id}.err"
        service._processes[process_id] = RunningProcess(
            process_id=process_id,
            argv=list(argv),
            process=child,
            stdout_path=stdout,
            stderr_path=stderr,
        )
        return {"pid": child.pid, "argv": list(argv), "exec_argv": list(argv)}

    monkeypatch.setattr(service, "_write_job", fail_running_manifest)
    monkeypatch.setattr(service, "start", fake_start)

    with pytest.raises(OSError, match="MANIFEST_DISK_FULL"):
        service.job_start([sys.executable, "-c", "print('must stop')"], "JWRITEFAIL")

    assert child.terminated is True
    assert child.returncode == -15
    assert service._read_job("JWRITEFAIL")["state"] == "CANCELLED"


@pytest.mark.parametrize("failure_phase", ["construction", "start"])
def test_failed_redaction_pump_setup_reaps_child_and_closes_pipes(
    tmp_path, monkeypatch, failure_phase
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)

    class Child:
        pid = 99992

        def __init__(self):
            self.returncode = None
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            assert timeout is not None
            return self.returncode

        def kill(self):
            self.returncode = -9

    class FailedThread:
        def __init__(self, **_kwargs):
            if failure_phase == "construction":
                raise RuntimeError("THREAD_CONSTRUCTION_FAILED")

        def start(self):
            raise RuntimeError("THREAD_START_FAILED")

    child = Child()
    monkeypatch.setattr(
        "easychange.core.process_service.subprocess.Popen",
        lambda *args, **kwargs: child,
    )
    monkeypatch.setattr(
        "easychange.core.process_service.threading.Thread", FailedThread
    )

    expected_error = (
        "THREAD_CONSTRUCTION_FAILED"
        if failure_phase == "construction"
        else "THREAD_START_FAILED"
    )
    with pytest.raises(RuntimeError, match=expected_error):
        service.start(
            [sys.executable, "-c", "print('must stop')"],
            "JTHREADFAIL",
            redact_values=["secret"],
        )

    assert child.terminated is True
    assert child.returncode == -15
    assert child.stdout.closed is True
    assert child.stderr.closed is True
    assert "JTHREADFAIL" not in service._processes


def test_failed_pump_cleanup_keeps_live_pid_tracked_for_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)

    class Child:
        pid = 99993

        def __init__(self):
            self.returncode = None
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.fail_terminate = True

        def poll(self):
            return self.returncode

        def terminate(self):
            if self.fail_terminate:
                raise OSError("TERMINATE_DENIED")
            self.returncode = -15

        def wait(self, timeout=None):
            assert timeout is not None
            return self.returncode

        def kill(self):
            self.returncode = -9

    class FailedThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("THREAD_START_FAILED")

    child = Child()
    monkeypatch.setattr(
        "easychange.core.process_service.subprocess.Popen",
        lambda *args, **kwargs: child,
    )
    monkeypatch.setattr(
        "easychange.core.process_service.threading.Thread", FailedThread
    )

    with pytest.raises(
        RuntimeError, match="PROCESS_PUMP_START_FAILED_CHILD_STILL_TRACKED"
    ):
        service.job_start(
            [sys.executable, "-c", "print('must remain tracked')"],
            "JTHREADRETRY",
            redact_values=["secret"],
        )

    assert service._processes["JTHREADRETRY"].process is child
    manifest = service._read_job("JTHREADRETRY")
    assert manifest["state"] == "RUNNING"
    assert manifest["pid"] == child.pid

    child.fail_terminate = False
    assert service.job_cancel("JTHREADRETRY")["state"] == "CANCELLED"


def test_concurrent_job_start_does_not_expose_queued_manifest_as_orphaned(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)
    start_entered = threading.Event()
    allow_start = threading.Event()
    second_call_entered = threading.Event()
    second_saw_queued = threading.Event()
    call_context = threading.local()
    start_calls = []
    original_read_job = service._read_job

    def observe_job_read(job_id):
        result = original_read_job(job_id)
        if (
            getattr(call_context, "second", False)
            and result
            and result.get("state") == "QUEUED"
        ):
            second_saw_queued.set()
        return result

    def blocked_start(argv, process_id, **kwargs):
        start_calls.append(process_id)
        start_entered.set()
        assert allow_start.wait(timeout=5)
        return {
            "pid": 12345,
            "state": "RUNNING",
            "argv": list(argv),
            "exec_argv": list(argv),
        }

    monkeypatch.setattr(service, "start", blocked_start)
    monkeypatch.setattr(service, "_read_job", observe_job_read)
    monkeypatch.setattr(service, "_pid_alive", lambda _pid: True)
    argv = [sys.executable, "-c", "print('once')"]

    def second_start():
        call_context.second = True
        second_call_entered.set()
        return service.job_start(argv, "JCONCUR01")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.job_start, argv, "JCONCUR01")
        assert start_entered.wait(timeout=5)
        second = pool.submit(second_start)
        assert second_call_entered.wait(timeout=5)

        # The first caller has published QUEUED but has not yet attached the
        # child process. The second caller must not observe it as an orphan.
        assert not second_saw_queued.wait(timeout=0.05)

        allow_start.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert not second_saw_queued.is_set()
    assert start_calls == ["JCONCUR01"]
    assert first_result["state"] == "RUNNING"
    assert second_result["state"] == "RUNNING"
    assert first_result["pid"] == second_result["pid"] == 12345


def test_concurrent_job_start_is_serialized_across_service_instances(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_service = ProcessService(workspace)
    second_service = ProcessService(workspace)
    start_entered = threading.Event()
    allow_start = threading.Event()
    second_saw_queued = threading.Event()
    start_calls = []
    original_read_job = second_service._read_job

    def observe_second_job_read(job_id):
        result = original_read_job(job_id)
        if result and result.get("state") == "QUEUED":
            second_saw_queued.set()
        return result

    def blocked_first_start(argv, process_id, **kwargs):
        start_calls.append(process_id)
        start_entered.set()
        assert allow_start.wait(timeout=5)
        return {
            "pid": 12345,
            "state": "RUNNING",
            "argv": list(argv),
            "exec_argv": list(argv),
        }

    monkeypatch.setattr(first_service, "start", blocked_first_start)
    monkeypatch.setattr(second_service, "_read_job", observe_second_job_read)
    monkeypatch.setattr(first_service, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(second_service, "_pid_alive", lambda _pid: True)
    argv = [sys.executable, "-c", "print('once')"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_service.job_start, argv, "JCONCUR02")
        assert start_entered.wait(timeout=5)
        second = pool.submit(second_service.job_start, argv, "JCONCUR02")

        # A separate service instance must wait for the first durable start
        # transaction instead of reconciling its live QUEUED manifest.
        assert not second_saw_queued.wait(timeout=0.05)

        allow_start.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert not second_saw_queued.is_set()
    assert start_calls == ["JCONCUR02"]
    assert first_result["state"] == "RUNNING"
    assert second_result["state"] == "RUNNING"
    assert first_result["pid"] == second_result["pid"] == 12345


def test_durable_job_start_lock_serializes_across_os_processes(tmp_path):
    job_path = tmp_path / "jobs" / "JLOCK01.json"
    child_job_path = (
        job_path.with_name(job_path.name.swapcase()) if os.name == "nt" else job_path
    )
    acquired_path = tmp_path / "child-acquired.txt"
    child_script = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from easychange.core.process_service import _durable_job_start_lock",
            "job_path, acquired_path = map(Path, sys.argv[1:])",
            "with _durable_job_start_lock(job_path):",
            "    acquired_path.write_text('yes')",
        )
    )

    with _durable_job_start_lock(job_path):
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_script,
                str(child_job_path),
                str(acquired_path),
            ],
            cwd=Path(__file__).resolve().parents[1],
        )
        time.sleep(0.1)
        blocked_while_owned = not acquired_path.exists()

    assert child.wait(timeout=5) == 0
    assert blocked_while_owned
    assert acquired_path.read_text() == "yes"


def test_job_status_does_not_orphan_an_inflight_start_from_another_instance(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_service = ProcessService(workspace)
    second_service = ProcessService(workspace)
    start_entered = threading.Event()
    allow_start = threading.Event()

    def blocked_start(argv, process_id, **kwargs):
        start_entered.set()
        assert allow_start.wait(timeout=5)
        return {
            "pid": 12345,
            "state": "RUNNING",
            "argv": list(argv),
            "exec_argv": list(argv),
        }

    monkeypatch.setattr(first_service, "start", blocked_start)
    monkeypatch.setattr(first_service, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(second_service, "_pid_alive", lambda _pid: True)
    argv = [sys.executable, "-c", "print('once')"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_service.job_start, argv, "JCONCUR03")
        assert start_entered.wait(timeout=5)
        status = pool.submit(second_service.job_status, "JCONCUR03")
        assert not status.done()

        allow_start.set()
        first_result = first.result(timeout=5)
        status_result = status.result(timeout=5)

    assert first_result["state"] == "RUNNING"
    assert status_result["state"] == "RUNNING"


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


def test_job_cancel_wins_over_concurrent_stale_finalizer(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)
    job_id = "JCANCELRACE1"
    manifest = {"job_id": job_id, "state": "RUNNING", "started_at": time.time()}
    natural_read_logs = threading.Event()
    allow_natural_finalizer = threading.Event()

    def read_job(_job_id):
        return dict(manifest)

    def write_job(_job_id, data):
        manifest.update(data)
        return data

    def tail_file(_path, _limit):
        if threading.current_thread().name.startswith("natural-finalizer"):
            natural_read_logs.set()
            assert allow_natural_finalizer.wait(timeout=5)
        return "", 0, "0" * 64

    monkeypatch.setattr(service, "_read_job", read_job)
    monkeypatch.setattr(service, "_write_job", write_job)
    monkeypatch.setattr(service, "_tail_file", tail_file)
    item = RunningProcess(
        process_id=job_id,
        argv=["fake"],
        process=type("ExitedProcess", (), {"pid": 123, "poll": lambda self: 0})(),
        stdout_path=workspace / "stdout.log",
        stderr_path=workspace / "stderr.log",
    )

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="natural-finalizer") as pool:
        natural = pool.submit(service._finalize_job, job_id, item)
        assert natural_read_logs.wait(timeout=5)
        cancelled = service._finalize_job(job_id, item, cancelled=True)
        allow_natural_finalizer.set()
        natural_result = natural.result(timeout=5)

    assert cancelled["state"] == "CANCELLED"
    assert natural_result["state"] == "CANCELLED"
    assert manifest["state"] == "CANCELLED"


def test_job_tail_returns_only_new_bytes_and_preserves_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)

    service.job_start(
        [
            sys.executable,
            "-c",
            (
                "import time;"
                "print('FIRST', flush=True);"
                "time.sleep(0.25);"
                "print('SECOND', flush=True)"
            ),
        ],
        "JTAIL01",
    )

    deadline = time.time() + 5
    first = None
    while time.time() < deadline:
        page = service.job_tail("JTAIL01", cursor=0, max_bytes=4096)
        if "FIRST" in page["text"]:
            first = page
            break
        time.sleep(0.02)
    assert first is not None
    cursor = first["next_cursor"] or first["total_bytes"]
    assert first["state"] in {"RUNNING", "SUCCEEDED"}

    deadline = time.time() + 5
    second = None
    while time.time() < deadline:
        page = service.job_tail("JTAIL01", cursor=cursor, max_bytes=4096)
        if "SECOND" in page["text"]:
            second = page
            break
        time.sleep(0.02)
    assert second is not None
    assert "FIRST" not in second["text"]
    assert "SECOND" in second["text"]
    assert second["cursor"] == cursor

    end_cursor = second["next_cursor"] or second["total_bytes"]
    empty = service.job_tail("JTAIL01", cursor=end_cursor, max_bytes=4096)
    assert empty["text"] == ""
    assert empty["returned_bytes"] == 0


def test_job_tail_redacts_secret_from_incremental_output(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)
    secret = "TOP-SECRET-JOB-TAIL"

    service.job_start(
        [sys.executable, "-c", f"print({secret!r})"],
        "JTAILSEC1",
        redact_values=[secret],
    )

    deadline = time.time() + 5
    page = None
    while time.time() < deadline:
        page = service.job_tail("JTAILSEC1", cursor=0, max_bytes=4096)
        if page["terminal"] or page["text"]:
            break
        time.sleep(0.02)

    assert page is not None
    assert secret not in page["text"]
    assert "[REDACTED]" in page["text"]
