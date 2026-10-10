"""Planned batch preflight must use the FileService text-file byte budget."""
from __future__ import annotations

import easychange.core.command_service as commands
from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def _replace(path):
    return {
        "op": "batch",
        "operations": [{
            "type": "replace_exact",
            "path": path,
            "old_text": "before",
            "new_text": "after!",
            "expected_occurrences": 1,
        }],
    }


def test_planned_mutation_rejects_oversized_source_before_transaction(tmp_path, monkeypatch):
    target = tmp_path / "oversized.txt"
    original = "before\n" + "x" * 128
    target.write_text(original, encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    try:
        monkeypatch.setattr(commands, "MAX_TEXT_FILE_BYTES", 16)
        result = service._execute_structured(_replace("oversized.txt"))
        assert result.ok is False
        assert result.code == "FILE_TOO_LARGE"
        assert result.data["transaction"]["state"] == "NOT_STARTED"
        assert target.read_text(encoding="utf-8") == original
        assert service.transaction_id is None
    finally:
        service.close()


def test_planned_mutation_accepts_existing_small_source_with_same_budget(tmp_path, monkeypatch):
    target = tmp_path / "small.txt"
    target.write_text("before\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    try:
        monkeypatch.setattr(commands, "MAX_TEXT_FILE_BYTES", 16)
        result = service._execute_structured(_replace("small.txt"))
        assert result.ok is True
        assert target.read_text(encoding="utf-8") == "after!\n"
    finally:
        service.close()
