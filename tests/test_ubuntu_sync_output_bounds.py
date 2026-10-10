"""Bound synchronous WSL stdout/stderr while preserving total byte counts."""
from __future__ import annotations

import io
import subprocess
from unittest.mock import patch

import pytest
from easychange.core.ubuntu_service import UbuntuService, UbuntuError


class FakeWSLProcess:
    def __init__(self, stdout=b"", stderr=b"", *, times_out=False):
        self.stdout=io.BytesIO(stdout)
        self.stderr=io.BytesIO(stderr)
        self.times_out=times_out
        self.wait_calls=[]
        self.killed=False

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.times_out and len(self.wait_calls)==1:
            raise subprocess.TimeoutExpired("wsl.exe",timeout)
        return 0

    def kill(self):
        self.killed=True


def test_synchronous_wsl_drains_both_pipes_without_retaining_full_output(monkeypatch):
    service=UbuntuService(None)
    monkeypatch.setattr(service,"_wsl_argv",
                        lambda session, argv:["wsl.exe","--exec",*argv])
    process=FakeWSLProcess(stdout=b"x"*20000,stderr=b"y"*9000)
    with patch("easychange.core.ubuntu_service.subprocess.Popen",
               return_value=process) as launched:
        result=service._run_sync(None,["kubectl","get","pods"],
                                 timeout=4,max_bytes=1024)
    assert result["return_code"]==0
    assert result["timed_out"] is False
    assert result["stdout_bytes"]==20000
    assert result["stderr_bytes"]==9000
    assert result["stdout"]=="x"*1024
    assert result["stderr"]=="y"*1024
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
    assert launched.call_args.kwargs["stdout"]==subprocess.PIPE
    assert launched.call_args.kwargs["stderr"]==subprocess.PIPE
    assert launched.call_args.kwargs["stdin"]==subprocess.DEVNULL
    assert process.wait_calls==[4]


def test_synchronous_wsl_timeout_kills_and_reaps_without_losing_bounded_logs(monkeypatch):
    service=UbuntuService(None)
    monkeypatch.setattr(service,"_wsl_argv",
                        lambda session, argv:["wsl.exe","--exec",*argv])
    process=FakeWSLProcess(stdout=b"a"*1800,stderr=b"timeout\n",
                           times_out=True)
    with patch("easychange.core.ubuntu_service.subprocess.Popen",
               return_value=process):
        result=service._run_sync(None,["kubectl","logs","pod"],
                                 timeout=1.5,max_bytes=1024)
    assert process.killed is True
    assert process.wait_calls==[1.5,3]
    assert result["timed_out"] is True
    assert result["return_code"] is None
    assert result["stdout_bytes"]==1800
    assert result["stdout"]=="a"*1024
    assert result["stdout_truncated"] is True
    assert result["stderr"]=="timeout\n"


def test_failure_to_start_drain_thread_reaps_wsl_child(monkeypatch):
    service=UbuntuService(None)
    monkeypatch.setattr(service,"_wsl_argv",
                        lambda session, argv:["wsl.exe","--exec",*argv])
    process=FakeWSLProcess(stdout=b"stalled",stderr=b"")
    with patch("easychange.core.ubuntu_service.subprocess.Popen",
               return_value=process), \
         patch("easychange.core.ubuntu_service.threading.Thread.start",
               side_effect=RuntimeError("THREAD_QUOTA_EXCEEDED")):
        with pytest.raises(UbuntuError,match="UBUNTU_OUTPUT_PUMP_START_FAILED"):
            service._run_sync(None,["kubectl","get","pods"],max_bytes=1024)
    assert process.killed is True
    assert process.wait_calls==[3]
    assert process.stdout.closed and process.stderr.closed
