"""FileService.replace must reject ambiguous search and replacement limits."""
import pytest

from easychange.core.file_service import FileService
from easychange.core.workspace import Workspace


@pytest.mark.parametrize("old,count", [("", 0), ("one", -1), ("one", True)])
def test_replace_rejects_ambiguous_input_without_mutating_file(tmp_path, old, count):
    path = tmp_path / "input.txt"
    path.write_text("one one\n", encoding="utf-8")
    service = FileService(Workspace(tmp_path, tmp_path.name))
    with pytest.raises((ValueError, TypeError)):
        service.replace("input.txt", old, "NEW", count)
    assert path.read_text(encoding="utf-8") == "one one\n"


def test_replace_valid_limit_reports_actual_replacements(tmp_path):
    path = tmp_path / "input.txt"
    path.write_text("one one one\n", encoding="utf-8")
    service = FileService(Workspace(tmp_path, tmp_path.name))
    result = service.replace("input.txt", "one", "NEW", 2)
    assert result["replacements"] == 2
    assert path.read_text(encoding="utf-8") == "NEW NEW one\n"
