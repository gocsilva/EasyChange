from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from easychange.core.command_service import CommandService
from easychange.core.git_service import GitService
from easychange.core.process_service import ProcessService
from easychange.core.runtime_service import ProjectRuntimeService
from easychange.core.workspace import Workspace


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "easychange@example.invalid")
    _git(root, "config", "user.name", "EasyChange Tests")


def test_workspace_excludes_easychange_without_touching_gitignore(tmp_path):
    _init_repo(tmp_path)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("bin/\n", encoding="utf-8")

    workspace = Workspace.open(tmp_path)
    runtime_dir = workspace.root_path / ".easychange"
    runtime_dir.mkdir(exist_ok=True)
    (runtime_dir / "runtime_profiles.json").write_text("{}", encoding="utf-8")

    status = _git(tmp_path, "status", "--short")
    assert ".easychange" not in status
    assert gitignore.read_text(encoding="utf-8") == "bin/\n"
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".easychange/" in exclude


def test_process_runtime_root_is_external_to_workspace(tmp_path, monkeypatch):
    localapp = tmp_path / "localapp"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(localapp))

    service = ProcessService(workspace_dir)

    assert workspace_dir not in service.root.parents
    assert str(service.root).startswith(str(localapp))
    assert service.jobs_root.parent == service.root.parent


def test_runtime_env_refs_do_not_persist_or_return_secret(tmp_path, monkeypatch):
    localapp = tmp_path / "localapp"
    monkeypatch.setenv("LOCALAPPDATA", str(localapp))
    monkeypatch.setenv("EASYCHANGE_TEST_DB_PASSWORD", "super-secret-value")
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    workspace = Workspace.open(workspace_dir)
    processes = ProcessService(workspace.root_path)
    runtime = ProjectRuntimeService(workspace, processes)

    configured = runtime.configure_profile(
        "secure-test",
        kind="custom",
        test_argv=[
            sys.executable,
            "-c",
            (
                "import os;"
                "print(os.environ.get('ASPNETCORE_ENVIRONMENT'));"
                "print('password-present=' + str(bool(os.environ.get('DB_PASSWORD'))))"
            ),
        ],
        env={"ASPNETCORE_ENVIRONMENT": "hml"},
        env_refs={"DB_PASSWORD": "PROCESS_ENV:EASYCHANGE_TEST_DB_PASSWORD"},
        inherit_env=False,
        env_allowlist=["PATH"],
    )

    config_text = runtime.config_path.read_text(encoding="utf-8")
    assert "super-secret-value" not in config_text
    assert "super-secret-value" not in json.dumps(configured, ensure_ascii=False)

    result = runtime.smart_test("secure-test", timeout=30)
    payload = json.dumps(result, ensure_ascii=False)
    assert result["passed"] is True
    assert "hml" in payload
    assert "password-present=True" in payload
    assert "super-secret-value" not in payload

    profile = runtime.profile("secure-test")
    assert profile["env"]["ASPNETCORE_ENVIRONMENT"] == "hml"
    assert profile["env_refs"]["DB_PASSWORD"] == "PROCESS_ENV:EASYCHANGE_TEST_DB_PASSWORD"
    assert profile["resolved_env_keys"] == ["ASPNETCORE_ENVIRONMENT", "DB_PASSWORD"]
    assert "super-secret-value" not in json.dumps(profile, ensure_ascii=False)


def test_git_diff_refs_name_status_is_paginated_without_silent_truncation(tmp_path):
    _init_repo(tmp_path)
    for index in range(12):
        (tmp_path / f"file-{index:02d}.txt").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    for index in range(12):
        (tmp_path / f"file-{index:02d}.txt").write_text(f"changed-{index}\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "change")
    head = _git(tmp_path, "rev-parse", "HEAD")

    service = GitService(Workspace.open(tmp_path))
    first = service.diff_refs(
        base_ref=base,
        head_ref=head,
        name_status=True,
        cursor=0,
        max_items=5,
    )
    second = service.diff_refs(
        base_ref=base,
        head_ref=head,
        name_status=True,
        cursor=first["next_cursor"],
        max_items=5,
    )
    third = service.diff_refs(
        base_ref=base,
        head_ref=head,
        name_status=True,
        cursor=second["next_cursor"],
        max_items=5,
    )

    combined = first["items"] + second["items"] + third["items"]
    assert len(combined) == 12
    assert first["complete"] is False
    assert second["complete"] is False
    assert third["complete"] is True
    assert third["remaining_count"] == 0
    assert len({item["path"] for item in combined}) == 12


def test_git_diff_refs_patch_uses_byte_cursor(tmp_path):
    _init_repo(tmp_path)
    target = tmp_path / "large.txt"
    target.write_text("\n".join(f"line-{i}" for i in range(300)) + "\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    target.write_text("\n".join(f"changed-{i}" for i in range(300)) + "\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "change")
    head = _git(tmp_path, "rev-parse", "HEAD")

    service = GitService(Workspace.open(tmp_path))
    pages = []
    cursor = 0
    for _ in range(20):
        page = service.diff_refs(
            base_ref=base,
            head_ref=head,
            path="large.txt",
            cursor=cursor,
            max_bytes=900,
            unified_lines=1,
        )
        pages.append(page["text"])
        if page["complete"]:
            break
        cursor = page["next_cursor"]

    assert len(pages) > 1
    assert all(len(chunk.encode("utf-8")) <= 900 for chunk in pages[:-1])
    assert page["complete"] is True
    assert page["remaining_bytes"] == 0


def test_runtime_secret_output_is_redacted(tmp_path, monkeypatch):
    localapp = tmp_path / "localapp"
    monkeypatch.setenv("LOCALAPPDATA", str(localapp))
    monkeypatch.setenv("EASYCHANGE_TEST_SECRET", "do-not-leak-12345")
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    workspace = Workspace.open(workspace_dir)
    processes = ProcessService(workspace.root_path)
    runtime = ProjectRuntimeService(workspace, processes)
    runtime.configure_profile(
        "redaction-test",
        test_argv=[
            sys.executable,
            "-c",
            "import os; print('value=' + os.environ['PRIVATE_VALUE'])",
        ],
        env_refs={"PRIVATE_VALUE": "PROCESS_ENV:EASYCHANGE_TEST_SECRET"},
        inherit_env=False,
    )

    result = runtime.smart_test("redaction-test", timeout=30)
    payload = json.dumps(result, ensure_ascii=False)
    assert result["passed"] is True
    assert "do-not-leak-12345" not in payload
    assert "[REDACTED]" in payload



def test_runtime_environment_resolution_status_is_compact_and_secret_free(tmp_path, monkeypatch):
    workspace = Workspace.open(tmp_path)
    service = CommandService(workspace)
    try:
        service.runtime.configure_profile(
            "api-hml",
            kind="dotnet-api",
            run_argv=["dotnet", "run"],
            env={"STATIC_KEY": "visible-config"},
            env_refs={
                "ConnectionString": "MCP_HOST_ENV:ACAM_HML_CONNECTION",
                "OtherKey": "MCP_HOST_ENV:OTHER_VALUE",
            },
        )
        monkeypatch.setattr(
            service.runtime,
            "external_env_provider",
            lambda: {"ACAM_HML_CONNECTION": "SECRET-VALUE-THAT-MUST-NOT-LEAK"},
        )
        result = service._execute_structured({
            "op": "operation",
            "type": "runtime_environment_status",
            "profile": "api-hml",
            "env_key": "ConnectionString",
        })
        assert result.ok is True
        primary = result.data["primary"]["data"]
        assert primary["compact"] is True
        assert primary["all_refs_resolved"] is False
        assert primary["resolved_ref_keys"] == ["ConnectionString"]
        assert primary["unresolved_ref_keys"] == ["OtherKey"]
        assert primary["selected"]["configured"] is True
        assert primary["selected"]["resolved"] is True
        assert primary["secret_values_returned"] is False
        serialized = json.dumps(primary, ensure_ascii=False)
        assert "SECRET-VALUE-THAT-MUST-NOT-LEAK" not in serialized
        assert "STATIC_KEY" not in serialized
    finally:
        service.close()


def test_runtime_environment_resolution_status_can_query_ref_name(tmp_path, monkeypatch):
    workspace = Workspace.open(tmp_path)
    service = CommandService(workspace)
    try:
        service.runtime.configure_profile(
            "api-hml",
            kind="dotnet-api",
            run_argv=["dotnet", "run"],
            env_refs={"ConnectionString": "MCP_HOST_ENV:ACAM_HML_CONNECTION"},
        )
        monkeypatch.setattr(
            service.runtime,
            "external_env_provider",
            lambda: {"ACAM_HML_CONNECTION": "secret"},
        )
        status = service.runtime.environment_resolution_status(
            "api-hml",
            ref_name="ACAM_HML_CONNECTION",
        )
        assert status["selected"]["configured"] is True
        assert status["selected"]["resolved"] is True
        assert status["selected"]["env_keys"] == ["ConnectionString"]
    finally:
        service.close()


def test_git_diff_refs_patch_preserves_utf8_when_page_splits_long_line(tmp_path):
    _init_repo(tmp_path)
    target=tmp_path/"unicode-long.txt"
    target.write_text("old\n",encoding="utf-8")
    _git(tmp_path,"add",".")
    _git(tmp_path,"commit","-m","base")
    base=_git(tmp_path,"rev-parse","HEAD")
    target.write_text("€"*1000+"\n",encoding="utf-8")
    _git(tmp_path,"add",".")
    _git(tmp_path,"commit","-m","utf8")
    head=_git(tmp_path,"rev-parse","HEAD")
    expected=subprocess.run(
        ["git","diff","--no-ext-diff","--unified=0",
         f"{base}...{head}","--","unicode-long.txt"],
        cwd=tmp_path,capture_output=True,check=True,
    ).stdout.decode("utf-8")
    service=GitService(Workspace.open(tmp_path))
    cursor=0
    parts=[]
    for _ in range(40):
        page=service.diff_refs(base_ref=base,head_ref=head,
                path="unicode-long.txt",unified_lines=0,max_bytes=256,cursor=cursor)
        parts.append(page["text"])
        if page["complete"]:break
        assert page["next_cursor"]>cursor
        cursor=page["next_cursor"]
    assert page["complete"]
    assert "\ufffd" not in "".join(parts)
    assert "".join(parts).strip()==expected.strip()
