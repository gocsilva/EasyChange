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
    dotnet = tmp_path / "dotnet"
    dotnet.mkdir()
    (dotnet / "Example.csproj").write_text("<Project />", encoding="utf-8")
    assert Workspace.open(dotnet).kind == "DOTNET"


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


def test_external_change_persistence_redo_and_transaction_recovery(service, tmp_path):
    original = (tmp_path / "sample.py").read_text(encoding="utf-8")
    assert service.execute(":read sample.py").ok
    (tmp_path / "sample.py").write_text("external = True\n", encoding="utf-8")
    conflict = service.execute(':write sample.py "ours"')
    assert conflict.code == "EXTERNAL_CHANGE"
    assert service.execute(':force-write sample.py "ours"').ok
    replacement = CommandService(Workspace.open(tmp_path))
    assert replacement.execute(":undo").ok
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "external = True\n"
    assert replacement.execute(":redo").ok
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "ours"
    replacement.execute(":begin")
    replacement.execute(':write sample.py "transaction"')
    resumed = CommandService(Workspace.open(tmp_path))
    assert resumed.transaction_id is not None
    assert resumed.execute(":rollback").ok
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "ours"
    assert original


def test_batch_alias_paging_macro_symbols_and_file_metadata(service, tmp_path):
    (tmp_path / "module.py").write_text("class Example:\n    def run(self):\n        return 1\n", encoding="utf-8")
    result = service.execute(':batch s ext:py Example ; outline module.py')
    assert result.ok and result.data["count"] == 2
    symbols = service.execute(":definition Example")
    assert symbols.data["symbols"][0]["kind"] == "class"
    metadata = service.execute(":stat module.py")
    assert metadata.data["kind"] == "text"
    service.execute(":alias x state")
    assert service.execute(":x").ok
    service.execute(':macro define readit "read sample.py 1 1"')
    assert service.execute(":macro run readit").ok


def test_locks_are_shared_and_search_index_incremental(service, tmp_path):
    assert service.execute(":lock sample.py 60").ok
    other = CommandService(Workspace.open(tmp_path))
    blocked = other.execute(':write sample.py "blocked"')
    assert blocked.code == "LOCKED"
    assert service.execute(":unlock sample.py").ok
    before = service.indexer.refresh()
    (tmp_path / "sample.py").write_text("changed phrase", encoding="utf-8")
    matches = service.execute(":search regex:changed ext:py")
    assert matches.ok and matches.data["matches"]
    assert before["files"] >= 1


def test_high_level_rename_scaffold_git_router_and_clear(service, tmp_path):
    (tmp_path / "module.py").write_text("class OldName:\n    value = OldName\n", encoding="utf-8")
    renamed = service.execute(":rename-symbol OldName NewName")
    assert renamed.ok and renamed.data["replacements"] == 2
    assert "NewName" in (tmp_path / "module.py").read_text(encoding="utf-8")
    assert service.execute(":undo").ok
    assert "OldName" in (tmp_path / "module.py").read_text(encoding="utf-8")
    assert service.execute(":create-interface IExample").ok
    assert "Protocol" in (tmp_path / "IExample.py").read_text(encoding="utf-8")
    assert service.execute(":git status").code == "ADAPTER_UNAVAILABLE"
    service.execute(":search OldName")
    assert service.execute(":clear").data["cleared"]


def test_compact_hid_results_keep_machine_readable_data():
    from easychange.core.result import Result
    import json

    rendered = Result(True, "search", {"matches": [{"id": "R1", "file": "a.py"}]}, command_id="C4").render("compact")
    payload = json.loads(rendered.split(" ", 4)[4])
    assert payload["matches"][0]["id"] == "R1"
    failed = Result(False, "write", error="conflict", code="EXTERNAL_CHANGE", command_id="C5").render("compact")
    assert "EXTERNAL_CHANGE" in failed and "conflict" in failed


def test_remote_quickstart_contains_local_gui_and_stdio_mcp_config(tmp_path):
    import sys
    from easychange.remote.agent_profile import remote_quickstart

    setup = remote_quickstart(tmp_path, sys.executable)
    assert "--machine --hid" in setup["gui_command"]
    assert setup["mcp_config"]["transport"] == "stdio"
    assert setup["mcp_config"]["args"][-1] == str(tmp_path.resolve())


def test_remote_boot_card_and_prepare_hid(service):
    boot = service.execute(":remote")
    assert boot.ok and boot.data["next"] == [":state", ":capabilities", ":remote-guide"]
    prepared = service.execute(":prepare-hid")
    assert prepared.ok and prepared.data["transport"] == "hid"
    assert service.machine and service.output == "compact"
