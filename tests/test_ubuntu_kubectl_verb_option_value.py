"""Kubectl verb must be the actual command, never an option value or plugin argument."""
from __future__ import annotations

import pytest

from easychange.core.ubuntu_service import UbuntuError, UbuntuService, UbuntuSession


class NeverStart:
    def __init__(self):
        self.started = False
    def job_start(self, *args, **kwargs):
        self.started = True
        raise AssertionError("unapproved kubectl job launched")


@pytest.mark.parametrize("argv,expected", [
    (["-n", "get", "delete", "pod", "victim"], ("delete", "pod")),
    (["--namespace", "get", "apply", "-f", "resource.yaml"], ("apply", "")),
    (["malicious-plugin", "get"], ("", "")),
    (["-n", "hml", "get", "pods"], ("get", "pods")),
    (["--namespace=get", "rollout", "restart", "deployment/demo"], ("rollout", "restart")),
])
def test_kubectl_parser_uses_real_command_not_option_values(argv,expected):
    assert UbuntuService._kubectl_verb(argv) == expected


@pytest.mark.parametrize("argv", [
    ["-n", "get", "delete", "pod", "victim"],
    ["--namespace", "get", "apply", "-f", "resource.yaml"],
    ["malicious-plugin", "get"],
])
def test_kubectl_mutation_cannot_bypass_explicit_permission(argv):
    processes=NeverStart()
    service=UbuntuService(processes)
    service._sessions["hml"]=UbuntuSession(
        session_id="hml", distro="Ubuntu", cwd="/tmp",
        namespace="hml", context="aks-hml",
    )
    with pytest.raises(UbuntuError, match="KUBECTL_MUTATION_REQUIRES_ALLOW_MUTATION"):
        service.kubectl("hml",argv,job_id="JNO",allow_mutation=False)
    assert processes.started is False


@pytest.mark.parametrize("argv", [
    ["-n", "get", "get", "secret", "private", "-o", "json"],
    ["--namespace", "get", "get", "secrets", "-o", "yaml"],
])
def test_secret_read_guard_uses_actual_get_command(argv):
    with pytest.raises(UbuntuError,match="K8S_SECRET_VALUE_READ_FORBIDDEN"):
        UbuntuService._guard_secret_read(argv)
