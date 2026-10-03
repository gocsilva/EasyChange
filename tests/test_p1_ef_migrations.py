from __future__ import annotations

import json
from pathlib import Path

import pytest

from easychange.core.database_service import DatabaseService
from easychange.core.ef_migration_service import EfMigrationService
from easychange.core.process_service import ProcessService
from easychange.core.runtime_service import ProjectRuntimeService
from easychange.core.workspace import Workspace


def _service(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "Migrations.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"></Project>', encoding="utf-8"
    )
    (workspace_dir / "Startup.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>', encoding="utf-8"
    )
    workspace = Workspace.open(workspace_dir)
    processes = ProcessService(workspace.root_path)
    runtime = ProjectRuntimeService(workspace, processes)
    database = DatabaseService(workspace)
    service = EfMigrationService(workspace, processes, runtime, database)
    monkeypatch.setattr(service, "_dotnet", lambda: "dotnet")
    return service, processes, database, workspace_dir


def test_ef_migrations_list_builds_identifies_context_and_compares_history(tmp_path, monkeypatch):
    service, processes, database, _root = _service(tmp_path, monkeypatch)
    calls = []

    def fake_run(argv, timeout=300, **kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["build", "Startup.csproj"]:
            return {"returncode": 0, "duration_ms": 10, "stdout": "Build succeeded.", "stderr": ""}
        if argv[1:4] == ["ef", "dbcontext", "list"]:
            return {"returncode": 0, "stdout": "Acam.Namespace.AcamDbContext\n", "stderr": ""}
        if argv[1:4] == ["ef", "migrations", "list"]:
            return {
                "returncode": 0,
                "stdout": "202601010101_Initial\n202602020202_Add225\n",
                "stderr": "",
            }
        raise AssertionError(argv)

    history_calls = []

    def fake_query(connection, sql, **kwargs):
        history_calls.append((connection, sql))
        return {
            "rows": [["202601010101_Initial", "8.0.0"]],
            "row_count": 1,
            "truncated": False,
        }

    monkeypatch.setattr(processes, "run", fake_run)
    monkeypatch.setattr(database, "query", fake_query)

    result = service.migrations_list(
        migration_project="Migrations.csproj",
        startup_project="Startup.csproj",
        context="AcamDbContext",
        connection_alias="hml-acam",
    )

    assert result["context"] == "AcamDbContext"
    assert result["migrations"] == [
        "202601010101_Initial",
        "202602020202_Add225",
    ]
    assert result["pending_migrations"] == ["202602020202_Add225"]
    assert result["pending_count"] == 1
    assert result["secret_values_returned"] is False
    assert calls[0][1:3] == ["build", "Startup.csproj"]
    assert calls[1][1:4] == ["ef", "dbcontext", "list"]
    assert calls[2][1:4] == ["ef", "migrations", "list"]
    assert history_calls[0][0] == "hml-acam"
    assert "__EFMigrationsHistory" in history_calls[0][1]


def test_ef_database_update_is_fail_closed_without_authorization(tmp_path, monkeypatch):
    service, processes, database, _root = _service(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(processes, "run", lambda *args, **kwargs: calls.append(args) or {})
    monkeypatch.setattr(database, "query", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("database must not be touched")
    ))

    with pytest.raises(PermissionError, match="EXPLICIT_AUTHORIZATION"):
        service.database_update(
            migration_project="Migrations.csproj",
            startup_project="Startup.csproj",
            context="AcamDbContext",
            connection_alias="hml-acam",
            apply_authorized=False,
        )
    assert calls == []


def test_ef_database_update_verifies_history_after_apply(tmp_path, monkeypatch):
    service, processes, database, _root = _service(tmp_path, monkeypatch)
    calls = []

    def fake_run(argv, timeout=300, **kwargs):
        calls.append(list(argv))
        if argv[1] == "build":
            return {"returncode": 0, "duration_ms": 5, "stdout": "Build succeeded.", "stderr": ""}
        if argv[1:4] == ["ef", "dbcontext", "list"]:
            return {"returncode": 0, "stdout": "AcamDbContext\n", "stderr": ""}
        if argv[1:4] == ["ef", "migrations", "list"]:
            return {"returncode": 0, "stdout": "M1\nM2\n", "stderr": ""}
        if argv[1:4] == ["ef", "database", "update"]:
            return {"returncode": 0, "duration_ms": 20, "stdout": "Done.", "stderr": ""}
        raise AssertionError(argv)

    histories = [
        {"rows": [["M1", "8.0"]], "row_count": 1, "truncated": False},
        {"rows": [["M1", "8.0"], ["M2", "8.0"]], "row_count": 2, "truncated": False},
    ]

    def fake_query(*args, **kwargs):
        return histories.pop(0)

    monkeypatch.setattr(processes, "run", fake_run)
    monkeypatch.setattr(database, "query", fake_query)

    result = service.database_update(
        migration_project="Migrations.csproj",
        startup_project="Startup.csproj",
        context="AcamDbContext",
        connection_alias="hml-acam",
        apply_authorized=True,
    )

    assert result["applied"] is True
    assert result["verified"] is True
    assert result["history_before"]["applied_migrations"] == ["M1"]
    assert result["history_after"]["applied_migrations"] == ["M1", "M2"]
    assert any(argv[1:4] == ["ef", "database", "update"] for argv in calls)


def test_ef_script_is_external_and_cursor_paginated(tmp_path, monkeypatch):
    service, processes, database, workspace_root = _service(tmp_path, monkeypatch)

    def fake_run(argv, timeout=300, **kwargs):
        if argv[1] == "build":
            return {"returncode": 0, "duration_ms": 5, "stdout": "Build succeeded.", "stderr": ""}
        if argv[1:4] == ["ef", "dbcontext", "list"]:
            return {"returncode": 0, "stdout": "AcamDbContext\n", "stderr": ""}
        if argv[1:4] == ["ef", "migrations", "list"]:
            return {"returncode": 0, "stdout": "M1\nM2\n", "stderr": ""}
        if argv[1:4] == ["ef", "migrations", "script"]:
            output_index = argv.index("--output") + 1
            Path(argv[output_index]).write_text("SELECT 1;\n" * 300, encoding="utf-8")
            return {"returncode": 0, "duration_ms": 8, "stdout": "", "stderr": ""}
        raise AssertionError(argv)

    monkeypatch.setattr(processes, "run", fake_run)
    page1 = service.script(
        migration_project="Migrations.csproj",
        startup_project="Startup.csproj",
        context="AcamDbContext",
        max_bytes=512,
    )
    page2 = service.script(
        migration_project="Migrations.csproj",
        startup_project="Startup.csproj",
        context="AcamDbContext",
        cursor=page1["next_cursor"],
        max_bytes=512,
    )

    assert page1["complete"] is False
    assert page1["returned_bytes"] <= 512
    assert page2["cursor"] == page1["next_cursor"]
    assert service.script_root.exists()
    assert workspace_root not in service.script_root.parents
    assert ".easychange" not in str(service.script_root)
    assert "SELECT 1;" in page1["text"]
    assert page1["secret_values_returned"] is False
