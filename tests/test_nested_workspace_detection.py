from __future__ import annotations

from easychange.adapters.dotnet import DotnetAdapter
from easychange.core.command_service import _profile_command
from easychange.core.workspace import Workspace


def test_workspace_detects_nested_dotnet_solution_from_git_root(tmp_path):
    (tmp_path / ".git").mkdir()
    app = tmp_path / "src" / "bs2.cambio.easypay.acam"
    app.mkdir(parents=True)
    (app / "bs2.cambio.easypay.acam.sln").write_text("", encoding="utf-8")
    project = app / "Api"
    project.mkdir()
    (project / "Api.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>',
        encoding="utf-8",
    )

    workspace = Workspace.open(tmp_path)

    assert workspace.kind == "DOTNET"
    assert "GIT" in workspace.adapters
    assert "DOTNET" in workspace.adapters
    assert "src/bs2.cambio.easypay.acam/bs2.cambio.easypay.acam.sln" in workspace.projects


def test_dotnet_adapter_targets_single_nested_solution(tmp_path):
    app = tmp_path / "src" / "Acam"
    app.mkdir(parents=True)
    (app / "Acam.sln").write_text("", encoding="utf-8")
    obj = app / "obj"
    obj.mkdir()
    (obj / "project.assets.json").write_text("{}", encoding="utf-8")

    workspace = Workspace.open(tmp_path)
    adapter = DotnetAdapter()

    assert adapter.detect(workspace) is True
    assert adapter.build(workspace) == [
        "dotnet", "build", "src/Acam/Acam.sln", "--no-restore"
    ]
    assert adapter.test(workspace) == [
        "dotnet", "test", "src/Acam/Acam.sln", "--no-restore"
    ]


def test_profile_command_preserves_nested_dotnet_target(tmp_path):
    app = tmp_path / "nested"
    app.mkdir()
    (app / "Service.sln").write_text("", encoding="utf-8")

    workspace = Workspace.open(tmp_path)

    assert _profile_command(workspace, "build") == [
        "dotnet", "build", "nested/Service.sln"
    ]
    assert _profile_command(workspace, "test") == [
        "dotnet", "test", "nested/Service.sln"
    ]


def test_dotnet_adapter_does_not_guess_among_multiple_solutions(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir(); second.mkdir()
    (first / "One.sln").write_text("", encoding="utf-8")
    (second / "Two.sln").write_text("", encoding="utf-8")

    workspace = Workspace.open(tmp_path)
    adapter = DotnetAdapter()

    assert workspace.kind == "DOTNET"
    assert adapter._target(workspace) is None
    assert adapter.build(workspace) == ["dotnet", "build"]
