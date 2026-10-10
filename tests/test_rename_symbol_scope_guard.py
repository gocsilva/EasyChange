"""Never report a workspace-wide rename after scanning only a subset."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


@pytest.fixture
def editor(tmp_path):
    source = tmp_path / "module.py"
    source.write_text("class OldName:\n    pass\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    try:
        yield service, source
    finally:
        service.close()


def test_rename_rejects_more_than_100k_indexed_files_before_fetch_or_mutation(editor):
    service, source = editor
    original = source.read_bytes()
    with patch.object(service.symbol_service, "definition", return_value={"name": "OldName"}), \
         patch.object(service.indexer, "count", return_value=100_001), \
         patch.object(service.indexer, "files",
                      side_effect=AssertionError("WOULD_LOAD_A_TRUNCATED_FILE_LIST")) as rows:
        result = service.execute(":rename-symbol OldName NewName")
    assert not result.ok
    assert "RENAME_SYMBOL_INDEX_TOO_LARGE" in (result.error or "")
    rows.assert_not_called()
    assert source.read_bytes() == original
    assert service.transaction is None


def test_rename_rejects_index_changes_before_opening_transaction(editor):
    service, source = editor
    original = source.read_bytes()
    rows = [
        {"path": "module.py", "size": len(original), "language": "python"},
        {"path": "another.py", "size": 10, "language": "python"},
    ]
    with patch.object(service.symbol_service, "definition", return_value={"name": "OldName"}), \
         patch.object(service.indexer, "count", side_effect=[2, 3]), \
         patch.object(service.indexer, "files", return_value=rows):
        result = service.execute(":rename-symbol OldName NewName")
    assert not result.ok
    assert "RENAME_SYMBOL_INDEX_CHANGED" in (result.error or "")
    assert source.read_bytes() == original
    assert service.transaction is None
