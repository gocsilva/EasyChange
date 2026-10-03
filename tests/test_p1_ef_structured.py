from __future__ import annotations

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def test_ef_structured_list_and_script_are_registered(tmp_path, monkeypatch):
    service = CommandService(Workspace.open(tmp_path))
    try:
        monkeypatch.setattr(service.ef_migrations, "migrations_list", lambda **kwargs: {
            "migration_project": kwargs["migration_project"],
            "pending_count": 2,
            "secret_values_returned": False,
        })
        monkeypatch.setattr(service.ef_migrations, "script", lambda **kwargs: {
            "script_id": "EFS1",
            "cursor": kwargs["cursor"],
            "complete": True,
            "text": "SELECT 1;",
            "secret_values_returned": False,
        })

        listed = service._execute_structured({
            "op": "operation",
            "type": "ef_migrations_list",
            "migration_project": "Migrations.csproj",
            "connection_alias": "hml-acam",
        })
        assert listed.ok is True
        assert listed.data["results"][0]["data"]["pending_count"] == 2

        scripted = service._execute_structured({
            "op": "operation",
            "type": "ef_script",
            "migration_project": "Migrations.csproj",
            "cursor": 123,
            "max_bytes": 2048,
        })
        assert scripted.ok is True
        assert scripted.data["results"][0]["data"]["cursor"] == 123

        caps = service.execute(":capabilities").data["capabilities"]["ai_machine"]["structured_operations"]
        assert caps["operations"]["ef_migrations_list"]["family"] == "database"
        assert caps["operations"]["ef_script"]["continue_on_error_safe"] is True
        assert caps["operations"]["ef_database_update"]["continue_on_error_safe"] is False
    finally:
        service.close()


def test_ef_database_update_cannot_be_mixed_in_batch(tmp_path, monkeypatch):
    service = CommandService(Workspace.open(tmp_path))
    try:
        called = []
        monkeypatch.setattr(
            service.ef_migrations,
            "database_update",
            lambda **kwargs: called.append(kwargs) or {"applied": True},
        )
        result = service._execute_structured({
            "op": "batch",
            "continue_on_error": True,
            "operations": [
                {
                    "type": "ef_database_update",
                    "migration_project": "Migrations.csproj",
                    "connection_alias": "hml-acam",
                    "apply_authorized": True,
                },
                {"type": "runtime_profiles"},
            ],
        })
        assert result.ok is False
        assert result.code == "INVALID_STRUCTURED_PAYLOAD"
        assert "CONTINUE_ON_ERROR_REQUIRES_SAFE_READ_BATCH" in (result.error or "")
        assert called == []
    finally:
        service.close()


def test_ef_database_update_requires_explicit_authorization(tmp_path, monkeypatch):
    service = CommandService(Workspace.open(tmp_path))
    try:
        def denied(**kwargs):
            assert kwargs["apply_authorized"] is False
            raise PermissionError("EF_DATABASE_UPDATE_REQUIRES_EXPLICIT_AUTHORIZATION")

        monkeypatch.setattr(service.ef_migrations, "database_update", denied)
        result = service._execute_structured({
            "op": "operation",
            "type": "ef_database_update",
            "migration_project": "Migrations.csproj",
            "connection_alias": "hml-acam",
            "apply_authorized": False,
        })
        assert result.ok is False
        child = result.data["results"][0]
        assert child["code"] == "EXPLICIT_AUTHORIZATION_REQUIRED"
        assert "EXPLICIT_AUTHORIZATION" in (child["error"] or "")
    finally:
        service.close()
