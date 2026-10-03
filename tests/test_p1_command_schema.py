from __future__ import annotations

from easychange.core.command_metadata import COMMAND_NAMES, command_catalog
from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def test_help_replace_returns_actionable_signature(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        result = service.execute(":help replace")
        assert result.ok is True
        schema = result.data["schema"]
        assert schema["name"] == "replace"
        assert schema["syntax"] == "replace <path> <old_text> <new_text>"
        assert [item["name"] for item in schema["arguments"]] == [
            "path", "old_text", "new_text"
        ]
        assert schema["mutating"] is True
        assert schema["supports_transaction"] is True
        assert schema["supports_rollback"] is True
        assert schema["examples"]
    finally:
        service.close()


def test_command_schema_text_and_structured_paths_match(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        text_result = service.execute(":command-schema read-many")
        assert text_result.ok is True
        structured = service._execute_structured({
            "op": "operation",
            "type": "command_schema",
            "command": "read-many",
        })
        assert structured.ok is True
        structured_schema = structured.data["results"][0]["data"]
        assert text_result.data["schema"] == structured_schema
        assert "--cursor CURSOR" in structured_schema["syntax"]
        assert structured_schema["read_only"] is True
    finally:
        service.close()


def test_help_without_argument_preserves_command_list_and_capabilities(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        result = service.execute(":help")
        assert result.ok is True
        assert "commands" in result.data
        assert "capabilities" in result.data
        assert "replace" in result.data["commands"]
        assert "command-schema" in result.data["commands"]
    finally:
        service.close()


def test_all_commands_have_required_machine_metadata():
    catalog = command_catalog()
    assert len(catalog) == len(COMMAND_NAMES)
    assert len({item["name"] for item in catalog}) == len(COMMAND_NAMES)
    for item in catalog:
        for key in (
            "name", "description", "syntax", "arguments", "examples",
            "mutating", "read_only", "max_payload_bytes", "supports_sequence",
            "supports_transaction", "supports_rollback",
        ):
            assert key in item, (item["name"], key)
        assert item["read_only"] is not item["mutating"]
        assert item["max_payload_bytes"] >= 150


def test_unknown_command_schema_fails_closed(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        result = service._execute_structured({
            "op": "operation",
            "type": "command_schema",
            "command": "definitely-not-a-command",
        })
        assert result.ok is False
        child = result.data["results"][0]
        assert child["code"] == "UNKNOWN_COMMAND"
    finally:
        service.close()


def test_complete_exact_command_returns_schema_not_just_name(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        result = service.execute(":complete replace")
        assert result.ok is True
        assert result.data["candidates"] == [":replace"]
        schema = result.data["schema"]
        assert schema["syntax"] == "replace <path> <old_text> <new_text>"
        assert [item["name"] for item in schema["arguments"]] == [
            "path", "old_text", "new_text"
        ]
    finally:
        service.close()
