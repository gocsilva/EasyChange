from __future__ import annotations

from types import SimpleNamespace

import pytest

from easychange.core.command_service import CommandService
from easychange.core.ubuntu_service import UbuntuError, UbuntuService, UbuntuSession
from easychange.core.operation_registry import structured_operation_is_mutation
from easychange.core.workspace import Workspace


class _FakeProcesses:
    def __init__(self):
        self.started = []

    def job_start(self, argv, job_id, *, redact_values=None, metadata=None, no_window=False):
        self.started.append((list(argv), job_id, list(redact_values or []), dict(metadata or {}), bool(no_window)))
        return {"pid": 4321, "state": "RUNNING"}

    def job_status(self, job_id):
        return {
            "job_id": job_id,
            "pid": 4321,
            "state": "SUCCEEDED",
            "terminal": True,
            "returncode": 0,
            "metadata": {
                "family": "ubuntu",
                "ubuntu_session_id": "hml",
                "distro": "Ubuntu-24.04",
                "cwd": "/mnt/c",
            },
        }

    def job_logs(self, job_id, *, stream="stdout", cursor=0, max_bytes=65536):
        text = "ok\n" if stream == "stdout" else ""
        return {
            "job_id": job_id,
            "stream": stream,
            "cursor": cursor,
            "next_cursor": None,
            "returned_bytes": len(text),
            "total_bytes": len(text),
            "complete": True,
            "text": text,
        }

    def job_cancel(self, job_id):
        return {"job_id": job_id, "state": "CANCELLED", "terminal": True}


def _session(service: UbuntuService):
    service._sessions["hml"] = UbuntuSession(
        session_id="hml",
        distro="Ubuntu-24.04",
        cwd="/mnt/c",
        env={"KUBECONFIG": "/mnt/c/sources/bs2-kubeconfig/kube.yml"},
        namespace="hml",
        context="aks-hml",
        environment="hml",
    )


def test_open_prefers_ubuntu_and_returns_structured_transport(monkeypatch):
    service = UbuntuService(_FakeProcesses())
    monkeypatch.setattr(service, "_distros", lambda: ["Debian", "Ubuntu-24.04"])

    def fake_run(session, argv, **kwargs):
        if argv[:2] == ["kubectl", "version"]:
            return {"return_code": 0, "stdout": '{"clientVersion":{}}', "stderr": ""}
        if argv == ["kubectl", "config", "current-context"]:
            return {"return_code": 0, "stdout": "aks-hml\n", "stderr": ""}
        if argv == ["kubectl", "config", "get-contexts", "-o", "name"]:
            return {"return_code": 0, "stdout": "aks-hml\naks-prd\n", "stderr": ""}
        return {"return_code": 0, "stdout": "/mnt/c\n", "stderr": ""}

    monkeypatch.setattr(service, "_run_sync", fake_run)
    result = service.open(
        "hml",
        kubeconfig="/mnt/c/sources/bs2-kubeconfig/kube.yml",
        namespace="hml",
    )

    assert result["distro"] == "Ubuntu-24.04"
    assert result["kubectl"]["current_context"] == "aks-hml"
    assert result["transport"]["result"] == "EC2_RESULT_STORE_CHUNKS"
    assert result["transport"]["terminal_window_required"] is False
    assert result["env"]["KUBECONFIG"].endswith("kube.yml")


def test_wsl_argv_is_structured_and_env_is_argument_not_shell(monkeypatch):
    service = UbuntuService(_FakeProcesses())
    _session(service)
    monkeypatch.setattr(service, "_wsl_exe", lambda: r"C:\Windows\System32\wsl.exe")

    argv = service._wsl_argv(service._session("hml"), ["kubectl", "get", "pods"])

    assert argv[:6] == [
        r"C:\Windows\System32\wsl.exe",
        "--distribution", "Ubuntu-24.04",
        "--cd", "/mnt/c",
        "--exec",
    ]
    assert "env" in argv
    assert "KUBECONFIG=/mnt/c/sources/bs2-kubeconfig/kube.yml" in argv
    assert argv[-3:] == ["kubectl", "get", "pods"]
    assert "-lc" not in argv


def test_exec_uses_durable_process_job_and_pages_output(monkeypatch):
    processes = _FakeProcesses()
    service = UbuntuService(processes)
    _session(service)
    monkeypatch.setattr(service, "_wsl_exe", lambda: "wsl.exe")

    result = service.exec("hml", ["printf", "ok"], job_id="JUBUNTU1", wait_ms=1)

    assert result["terminal"] is True
    assert result["return_code"] == 0
    assert result["stdout"] == "ok\n"
    started_argv, job_id, _redacted, metadata, no_window = processes.started[0]
    assert job_id == "JUBUNTU1"
    assert started_argv[0] == "wsl.exe"
    assert metadata["family"] == "ubuntu"
    assert metadata["ubuntu_session_id"] == "hml"
    assert no_window is True


def test_kubectl_defaults_namespace_and_read_only_is_allowed(monkeypatch):
    service = UbuntuService(_FakeProcesses())
    _session(service)
    captured = {}

    def fake_exec_job(session, argv, **kwargs):
        captured["session_id"] = session.session_id
        captured["argv"] = list(argv)
        captured["kwargs"] = dict(kwargs)
        return {"terminal": True, "return_code": 0, "stdout": "pod/a\n", "stderr": ""}

    monkeypatch.setattr(service, "_exec_job", fake_exec_job)
    result = service.kubectl(
        "hml", ["get", "pods"],
        job_id="JK8S1",
        allow_mutation=False,
    )

    assert captured["argv"] == ["kubectl", "--context", "aks-hml", "--namespace", "hml", "get", "pods"]
    assert result["kubectl"]["mutation"] is False
    assert result["kubectl"]["namespace"] == "hml"


@pytest.mark.parametrize(
    "args",
    [
        ["delete", "job", "x"],
        ["create", "job", "x"],
        ["set", "env", "cronjob/x", "A=B"],
        ["rollout", "restart", "deploy/x"],
        ["exec", "pod/x", "--", "sh"],
    ],
)
def test_kubectl_mutations_require_explicit_flag(args):
    service = UbuntuService(_FakeProcesses())
    _session(service)
    with pytest.raises(UbuntuError, match="KUBECTL_MUTATION_REQUIRES_ALLOW_MUTATION"):
        service.kubectl("hml", args, job_id="JMUTATE", allow_mutation=False)


def test_kubectl_mutation_can_be_explicitly_allowed(monkeypatch):
    service = UbuntuService(_FakeProcesses())
    _session(service)
    captured = {}
    def fake_exec_job(session, argv, **kwargs):
        captured["call"] = (session.session_id, list(argv), kwargs)
        return {"terminal": True, "return_code": 0, "stdout": "deleted\n", "stderr": ""}
    monkeypatch.setattr(service, "_exec_job", fake_exec_job)

    result = service.kubectl(
        "hml", ["delete", "job", "tmp", "--ignore-not-found"],
        job_id="JDEL",
        allow_mutation=True,
        confirm_context="aks-hml",
    )
    assert result["kubectl"]["mutation"] is True
    assert captured["call"][1][:6] == ["kubectl", "--context", "aks-hml", "--namespace", "hml", "delete"]


def test_command_service_dispatches_k8s_get_without_terminal_focus(tmp_path, monkeypatch):
    service = CommandService(Workspace.open(tmp_path))
    try:
        captured = {}
        def fake_kubectl(session_id, args, **kwargs):
            captured["session_id"] = session_id
            captured["args"] = list(args)
            captured["kwargs"] = dict(kwargs)
            return {
                "terminal": True,
                "return_code": 0,
                "stdout": "NAME READY\npod-a 1/1\n",
                "stderr": "",
            }
        monkeypatch.setattr(service.ubuntu, "kubectl", fake_kubectl)

        result = service._execute_structured({
            "op": "operation",
            "type": "k8s_get",
            "session_id": "hml",
            "resource": "pods",
            "namespace": "hml",
            "output": "wide",
        })

        assert result.ok is True
        assert captured["session_id"] == "hml"
        assert captured["args"] == ["get", "pods", "-o", "wide"]
        assert result.data["execution_target"] == "REMOTE_PC_WSL"
        assert result.data["result_transport"] == "EC2_RESULT_STORE_CHUNKS"
    finally:
        service.close()


def test_kubectl_mutation_requires_exact_context_even_when_allowed():
    service = UbuntuService(_FakeProcesses())
    _session(service)
    with pytest.raises(
        UbuntuError,
        match="KUBECTL_MUTATION_REQUIRES_EXACT_CONTEXT_CONFIRMATION",
    ):
        service.kubectl(
            "hml",
            ["delete", "job", "tmp"],
            job_id="JCTX",
            allow_mutation=True,
            confirm_context="wrong-context",
        )


@pytest.mark.parametrize(
    "args",
    [
        ["get", "pods", "--token", "abc"],
        ["get", "pods", "--server=https://other.example"],
        ["get", "pods", "--kubeconfig=/tmp/other"],
        ["get", "pods", "--context", "prd"],
    ],
)
def test_kubectl_forbids_connection_and_context_overrides(args):
    service = UbuntuService(_FakeProcesses())
    _session(service)
    with pytest.raises(
        UbuntuError,
        match="KUBECTL_CONNECTION_OR_CONTEXT_OVERRIDE_FORBIDDEN",
    ):
        service.kubectl("hml", args, job_id="JOVERRIDE")


@pytest.mark.parametrize(
    "args",
    [
        ["get", "secret", "x", "-o", "json"],
        ["get", "secrets", "-o=yaml"],
        ["get", "secret/x", "-o", "jsonpath={.data.password}"],
    ],
)
def test_kubectl_blocks_raw_secret_value_reads(args):
    service = UbuntuService(_FakeProcesses())
    _session(service)
    with pytest.raises(UbuntuError, match="K8S_SECRET_VALUE_READ_FORBIDDEN"):
        service.kubectl("hml", args, job_id="JSECRET")


def test_kubectl_default_secret_table_read_is_allowed(monkeypatch):
    service = UbuntuService(_FakeProcesses())
    _session(service)
    monkeypatch.setattr(
        service,
        "_exec_job",
        lambda session, argv, **kwargs: {
            "terminal": True,
            "return_code": 0,
            "stdout": "NAME TYPE DATA AGE\n",
            "stderr": "",
        },
    )
    result = service.kubectl(
        "hml",
        ["get", "secrets"],
        job_id="JSECRETTABLE",
    )
    assert result["return_code"] == 0
    assert result["kubectl"]["mutation"] is False



def test_structured_mutation_profile_understands_ubuntu_and_k8s():
    assert structured_operation_is_mutation({
        "type": "k8s_command",
        "args": ["delete", "job", "tmp"],
        "allow_mutation": True,
    }) is True
    assert structured_operation_is_mutation({
        "type": "k8s_command",
        "args": ["get", "pods"],
        "allow_mutation": False,
    }) is False
    assert structured_operation_is_mutation({
        "type": "ubuntu_batch",
        "commands": [{"argv": ["getent", "hosts", "localhost"]}],
    }) is False
    assert structured_operation_is_mutation({
        "type": "ubuntu_batch",
        "commands": [{"command": "echo x > /tmp/x", "allow_mutation": True}],
    }) is True



def test_structured_mutation_profile_covers_non_file_side_effects():
    from easychange.core.operation_registry import (
        structured_capabilities,
        structured_operation_is_mutation,
    )

    assert structured_operation_is_mutation({"type": "browser_navigate", "url": "about:blank"})
    assert structured_operation_is_mutation({"type": "run_project", "profile": "api"})
    assert structured_operation_is_mutation({"type": "process_exec", "argv": ["dotnet", "--info"]})
    assert structured_operation_is_mutation({"type": "secret_broker_import", "source_name": "X"})
    assert structured_operation_is_mutation({"type": "ef_database_update", "connection_alias": "hml"})
    assert structured_operation_is_mutation({"type": "db_query", "allow_write": True})
    assert not structured_operation_is_mutation({"type": "db_query", "allow_write": False})
    assert structured_operation_is_mutation({"type": "ubuntu_exec", "allow_mutation": True})
    assert not structured_operation_is_mutation({"type": "ubuntu_exec", "allow_mutation": False})

    capabilities = structured_capabilities()["operations"]
    assert capabilities["browser_navigate"]["side_effect"] is True
    assert capabilities["db_query"]["conditional_side_effect"] is True
    assert capabilities["k8s_get"]["side_effect"] is False
    assert capabilities["read"]["conditional_side_effect"] is False
