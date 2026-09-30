from pathlib import Path

import pytest

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


@pytest.fixture
def service(tmp_path: Path):
    (tmp_path / "sample.py").write_text("NumeroProtocolo = 1\n", encoding="utf-8")
    return CommandService(Workspace.open(tmp_path))


def test_generic_workspace_search_read_and_edit(service, tmp_path):
    assert service.workspace.kind == "GENERIC"
    assert service.execute(":search NumeroProtocolo").data["matches"][0]["line"] == 1
    assert service.execute(":read sample.py").ok
    assert service.execute(':replace sample.py "= 1" "= 2"').ok
    assert "= 2" in (tmp_path / "sample.py").read_text(encoding="utf-8")


def test_undo_and_transaction_rollback(service, tmp_path):
    service.execute(':write sample.py "changed"')
    assert service.execute(":undo").ok
    assert "NumeroProtocolo" in (tmp_path / "sample.py").read_text(encoding="utf-8")
    service.execute(":begin")
    service.execute(':write sample.py "transaction value"')
    assert service.execute(":rollback").ok
    assert "NumeroProtocolo" in (tmp_path / "sample.py").read_text(encoding="utf-8")


def test_path_escape_is_rejected(service, tmp_path):
    result = service.execute(':read "../outside.txt"')
    assert not result.ok
    assert result.code == "PATH_OUTSIDE_WORKSPACE"


def test_detection_for_python_and_dotnet(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert Workspace.open(tmp_path).kind == "PYTHON"


def test_search_result_id_opens_near_match(service, tmp_path):
    (tmp_path / "sample.py").write_text("line 1\nline 2\nNumeroProtocolo\nline 4\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    search = service.execute(":search NumeroProtocolo")
    result_id = search.data["matches"][0]["id"]
    opened = service.execute(f":context {result_id}")
    assert opened.data["start"] == 1
    assert any("NumeroProtocolo" in line for line in opened.data["lines"])


def test_cli_one_shot_command(service, monkeypatch):
    from easychange import cli
    monkeypatch.chdir(service.workspace.root_path)
    assert cli.main(["state", "--json"]) == 0


def test_append_insert_and_binary_write_guard(service, tmp_path):
    assert service.execute(':append sample.py "tail = True"').ok
    assert "tail = True" in (tmp_path / "sample.py").read_text(encoding="utf-8")
    assert service.execute(':insert sample.py 1 "# header"').ok
    assert (tmp_path / "sample.py").read_text(encoding="utf-8").startswith("# header\n")
    (tmp_path / "image.bin").write_bytes(b"\x00\x01")
    blocked = service.execute(':write image.bin "no"')
    assert not blocked.ok
