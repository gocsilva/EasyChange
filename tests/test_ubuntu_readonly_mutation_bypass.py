"""Structured Ubuntu read-only admission must not allow mutation-capable argv."""
from __future__ import annotations

import pytest

from easychange.core.ubuntu_service import UbuntuError, UbuntuService, UbuntuSession


class NoRemoteProcess:
    def __init__(self):
        self.started = []
    def job_start(self, *args, **kwargs):
        self.started.append((args, kwargs))
        raise AssertionError("remote process must not be started")


@pytest.mark.parametrize("argv", [
    ["env", "rm", "-f", "/tmp/target"],
    ["env", "-i", "sh", "-c", "touch /tmp/target"],
    ["yq", "-i", ".flag = true", "data.yaml"],
    ["yq", "--inplace", ".flag = true", "data.yaml"],
    ["rg", "--pre", "sh -c 'touch /tmp/target'", "pattern", "."],
    ["rg", "--pre=sh -c 'touch /tmp/target'", "pattern", "."],
    ["ip", "link", "set", "dev", "eth0", "down"],
    ["ip", "netns", "exec", "test", "touch", "/tmp/target"],
    ["ss", "-K", "dst", "127.0.0.1"],
    ["hostname", "changed-name"],
    ["hostname", "-F", "/tmp/name"],
    ["date", "-s", "tomorrow"],
    ["date", "--set=tomorrow"],
])
def test_ubuntu_mutating_argv_requires_explicit_permission(argv):
    assert UbuntuService._ubuntu_read_only(argv) is False
    process = NoRemoteProcess()
    service = UbuntuService(process)
    service._sessions["safe"] = UbuntuSession(
        session_id="safe", distro="Ubuntu", cwd="/tmp"
    )
    with pytest.raises(UbuntuError, match="UBUNTU_COMMAND_REQUIRES_ALLOW_MUTATION"):
        service.exec("safe", argv, job_id="JSAFE", allow_mutation=False)
    assert process.started == []


@pytest.mark.parametrize("argv", [
    ["env"],
    ["yq", ".key", "data.yaml"],
    ["rg", "--files", "."],
    ["ip", "addr", "show"],
    ["ip", "-4", "route", "get", "127.0.0.1"],
    ["ss", "-tuln"],
    ["hostname"],
    ["hostname", "-f"],
    ["date", "+%F"],
    ["date", "-u", "+%F"],
])
def test_ubuntu_safe_read_argv_remains_available_without_mutation_permission(argv):
    assert UbuntuService._ubuntu_read_only(argv) is True
