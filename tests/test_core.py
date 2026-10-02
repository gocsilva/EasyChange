from pathlib import Path
import hashlib
import json

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


def test_sequenced_command_is_correlated_and_duplicate_is_suppressed(service, tmp_path):
    first = service.execute(":ec QABC123 :write sample.py 'safe = True'")
    assert first.ok and first.sequence == "QABC123"
    rendered = first.render("compact")
    assert rendered.startswith("EC1 QABC123 OK")
    duplicate = service.execute(":ec QABC123 :write sample.py 'safe = True'")
    assert duplicate.ok and duplicate.data["duplicate"] is True
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "safe = True"


def test_sequenced_command_conflict_and_unknown_are_never_replayed(service, tmp_path):
    first = service.execute(":ec QABC124 :write sample.py 'once = True'")
    assert first.ok
    conflict = service.execute(":ec QABC124 :write sample.py 'twice = True'")
    assert conflict.code == "SEQUENCE_CONFLICT"
    pending_command = ":write sample.py 'must_not_run = True'"
    service.state_store.data["ec_results"]["QABC125"] = {
        "sha256": hashlib.sha256(pending_command.encode("utf-8")).hexdigest(), "state": "RUNNING"
    }
    service._persist_state()
    unknown = service.execute(":ec QABC125 :write sample.py 'must_not_run = True'")
    assert unknown.code == "RUNNING"
    assert unknown.data["state"] == "RUNNING"
    assert "must_not_run" not in (tmp_path / "sample.py").read_text(encoding="utf-8")


def test_batch_is_atomic_and_rolls_back_on_first_failed_command(service, tmp_path):
    original = (tmp_path / "sample.py").read_text(encoding="utf-8")
    result = service.execute(":batch write sample.py 'temporary = True' ; write ../outside.txt 'bad' :end")
    assert not result.ok
    assert result.data["transaction"]["rolled_back"] == 1
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == original
    assert service.transaction is None


def test_ec1_compressed_command_round_trip_and_crc_guard(service, tmp_path):
    import json
    from easychange.core.transport_codec import encode_command

    command = ":write sample.py \"" + ("linha çã\n" * 120) + "\""
    packet, metadata = encode_command(command)
    assert metadata["encoding"] == "zlib+base64url"
    result = service.execute(f":ec QABC126 {packet}")
    assert result.sequence == "QABC126"
    assert result.ok
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == ("linha çã\n" * 120)
    header = packet.split(" ", 3)
    crc = ("0" if header[2][0] != "0" else "1") + header[2][1:]
    bad_packet = " ".join((header[0], header[1], crc, header[3]))
    invalid = service.execute(f":ec QABC127 {bad_packet}")
    assert invalid.code == "INVALID_COMPRESSED_PAYLOAD"


def test_ec1_structured_multiline_patch_is_journaled_and_transactional(service, tmp_path):
    import json
    from easychange.core.transport_codec import encode_command

    command = ":j1 " + json.dumps({"op": "operation", "type": "write_file", "path": "new.py",
                                   "content": "first = 'ç'\nsecond = 2\n"}, ensure_ascii=False)
    packet, _ = encode_command(command)
    result = service.execute(f":ec QABC128 {packet}")
    assert result.ok and result.sequence == "QABC128"
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "first = 'ç'\nsecond = 2\n"
    assert service.execute(f":ec QABC128 {packet}").data["duplicate"] is True


def test_ec1_structured_batch_rolls_back_prior_mutations(service, tmp_path):
    import json
    from easychange.core.transport_codec import encode_command

    original = (tmp_path / "sample.py").read_text(encoding="utf-8")
    command = ":j1 " + json.dumps({"op": "batch", "operations": [
        {"type": "write_file", "path": "sample.py", "content": "changed = True"},
        {"type": "unsupported", "path": "blocked"},
    ]})
    packet, _ = encode_command(command)
    result = service.execute(f":ec QABC129 {packet}")
    assert not result.ok and result.code == "STRUCTURED_BATCH_FAILED"
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == original
    assert service.transaction is None


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


def test_remote_quickstart_contains_local_gui_without_remote_mcp_config(tmp_path):
    import sys
    from easychange.remote.agent_profile import remote_guide_markdown, remote_quickstart

    setup = remote_quickstart(tmp_path, sys.executable)
    assert "--machine --hid" in setup["gui_command"]
    assert "mcp_config" not in setup
    assert "mcp_server_command" not in setup
    guide = remote_guide_markdown()
    assert "Do not start/connect EasyChange MCP/API on that computer" in guide


def test_remote_boot_card_and_prepare_hid(service):
    boot = service.execute(":remote")
    assert boot.ok and boot.data["next"] == [":state", ":capabilities", ":remote-guide"]
    prepared = service.execute(":prepare-hid")
    assert prepared.ok and prepared.data["transport"] == "hid"
    assert service.machine and service.output == "compact"


def test_locate_combines_context_and_result_ids_do_not_collide(service, tmp_path):
    (tmp_path / "sample.py").write_text("first\nNumeroProtocolo = 7\nlast\n", encoding="utf-8")
    located = service.execute(":locate NumeroProtocolo --context 1")
    hit = located.data["matches"][0]
    assert hit["id"] == "R1" and hit["context"] == ["1|first", "2|NumeroProtocolo = 7", "3|last"]
    restarted = CommandService(Workspace.open(tmp_path))
    later = restarted.execute(":search NumeroProtocolo")
    assert later.data["matches"][0]["id"] == "R2"
    original = restarted.execute(":read R1")
    assert original.data["start"] == 2
    edit = restarted.execute(':edit-result R2 "NumeroProtocolo = 8"')
    assert edit.ok and "= 8" in (tmp_path / "sample.py").read_text(encoding="utf-8")
    assert restarted.execute(":undo").ok



def test_search_ranks_source_definition_before_docs(service, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "ReceberLoteAcam225UseCase.cs").write_text(
        "public sealed class ReceberLoteAcam225UseCase {}\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "history.md").write_text(
        "ReceberLoteAcam225UseCase old notes\n", encoding="utf-8"
    )
    service.indexer.invalidate()
    result = service.execute(":search ReceberLoteAcam225UseCase")
    assert result.ok
    assert result.data["matches"][0]["file"].startswith("src/")


def test_symbol_index_reuses_persistent_sqlite_rows(service, tmp_path):
    (tmp_path / "module.py").write_text("class CachedExample:\n    pass\n", encoding="utf-8")
    service.indexer.invalidate()
    first = service.execute(":definition CachedExample")
    assert first.ok and first.data["symbols"]
    assert service.indexer.ready
    second = service.execute(":definition CachedExample")
    assert second.ok and second.data["symbols"] == first.data["symbols"]



def test_study_and_read_many_reduce_round_trips(service, tmp_path):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "Feature.cs").write_text(
        "public sealed class Feature {\n    public void Run() {}\n}\n", encoding="utf-8"
    )
    (tmp_path / "src" / "Caller.cs").write_text(
        "public sealed class Caller { Feature value = new Feature(); }\n", encoding="utf-8"
    )
    service.indexer.invalidate()
    study = service.execute(":study Feature --limit 6 --context 2")
    assert study.ok
    assert study.data["definitions"]
    assert study.data["files"][0].startswith("src/")
    many = service.execute(":read-many src/Feature.cs src/Caller.cs --count 40")
    assert many.ok and many.data["count"] == 2
    assert any("Feature" in line for line in many.data["files"][0]["lines"])


def test_structured_read_batch_does_not_open_transaction(service):
    result = service._execute_structured({
        "op": "batch",
        "operations": [
            {"type": "read", "path": "sample.py", "start": 1, "count": 5},
            {"type": "search", "query": "NumeroProtocolo"},
        ],
    })
    assert result.ok
    assert result.data["transaction"]["state"] == "NOT_REQUIRED"
    assert service.transaction is None


def test_ec1_session_state_stays_bounded_for_large_read_many(tmp_path):
    import json

    paths = []
    for index in range(8):
        path = tmp_path / f"Big{index}.cs"
        paths.append(path.name)
        path.write_text(
            "\n".join(f"public string P{line} => \"{index}-{line}-{'x' * 60}\";" for line in range(300)),
            encoding="utf-8",
        )
    service = CommandService(Workspace.open(tmp_path))
    packet = json.dumps({"op": "operation", "type": "read_many", "paths": paths, "count": 300},
                        separators=(",", ":"))
    for index in range(12):
        result = service.execute(f":ec QBLOAT{index:04d} :j1 " + packet)
        assert result.ok
    session_path = tmp_path / ".easychange" / "session.json"
    assert session_path.stat().st_size < 200_000
    persisted = json.loads(session_path.read_text(encoding="utf-8"))
    assert all("result" not in item for item in persisted["ec_results"].values())


def test_structured_patch_set_supports_range_text_insert_and_append(service, tmp_path):
    (tmp_path / "patch.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = service._execute_structured({
        "op": "batch",
        "operations": [
            {"type": "replace_range", "path": "patch.py", "start": 2, "end": 2, "content": "TWO"},
            {"type": "insert", "path": "patch.py", "line": 1, "content": "zero"},
            {"type": "append", "path": "patch.py", "content": "four"},
            {"type": "replace_text", "path": "patch.py", "old": "three", "new": "THREE"},
        ],
    })
    assert result.ok
    text = (tmp_path / "patch.py").read_text(encoding="utf-8")
    assert "zero" in text and "TWO" in text and "THREE" in text and "four" in text


def test_watcher_force_refresh_detects_external_change(service, tmp_path):
    from easychange.core.watcher import WorkspaceWatcher

    service.execute(":search NumeroProtocolo")
    (tmp_path / "sample.py").write_text("external watcher value\n", encoding="utf-8")
    watcher = WorkspaceWatcher(service.indexer)
    changed = watcher.poll_once()
    assert changed["indexed"] >= 1
    result = service.execute(":search external")
    assert result.ok and result.data["matches"]


def test_dotnet_adapter_uses_no_restore_when_assets_exist(tmp_path):
    from easychange.adapters.dotnet import DotnetAdapter

    (tmp_path / "Example.csproj").write_text("<Project />", encoding="utf-8")
    workspace = Workspace.open(tmp_path)
    adapter = DotnetAdapter()
    assert adapter.build(workspace) == ["dotnet", "build"]
    assets = tmp_path / "obj" / "project.assets.json"
    assets.parent.mkdir()
    assets.write_text("{}", encoding="utf-8")
    assert adapter.build(workspace) == ["dotnet", "build", "--no-restore"]
    assert adapter.test(workspace) == ["dotnet", "test", "--no-restore"]


def test_large_journal_snapshots_are_content_addressed_and_restart_safe(tmp_path):
    original = "\n".join(f"line {i} {'x' * 80}" for i in range(400)) + "\n"
    target = tmp_path / "large.txt"
    target.write_text(original, encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    replacement = original.replace("line 200", "LINE 200")
    assert service.files.write("large.txt", replacement, force=True)
    service.last_command = "write"
    service._record("large.txt", original, replacement)
    journal_path = tmp_path / ".easychange" / "journal.jsonl"
    assert journal_path.stat().st_size < len(original.encode("utf-8"))
    assert list((tmp_path / ".easychange" / "blobs").glob("*.zlib"))
    restarted = CommandService(Workspace.open(tmp_path))
    assert restarted.execute(":undo").ok
    assert target.read_text(encoding="utf-8") == original


def test_process_output_compaction_preserves_diagnostics(service):
    import sys

    result = service.processes.run([
        sys.executable,
        "-c",
        "print('noise' * 5000); print('ERROR CS1234: important failure')",
    ])
    assert result["output_truncated"] is True
    assert len(result["stdout"]) <= 12000
    assert "CS1234" in result["diagnostics"]



def test_git_watcher_reindexes_only_changed_paths(tmp_path):
    import subprocess
    from easychange.core.indexer import Indexer
    from easychange.core.watcher import WorkspaceWatcher

    source = tmp_path / "tracked.cs"
    source.write_text("class Before {}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "tracked.cs"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=EasyChange Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "initial"],
        cwd=tmp_path, check=True,
    )

    indexer = Indexer(tmp_path)
    indexer.refresh(force=True)
    watcher = WorkspaceWatcher(indexer)

    first = watcher.poll_once()
    assert first["backend"] == "git-incremental"
    assert first["indexed"] == 0

    source.write_text("class After {}\n", encoding="utf-8")
    changed = watcher.poll_once()
    assert changed["indexed"] == 1
    assert changed["files"] == 1

    unchanged = watcher.poll_once()
    assert unchanged["indexed"] == 0
    assert unchanged["files"] == 0
    assert unchanged["cached"] is True
    assert indexer.search("After")



def test_read_many_supports_32_paths_with_byte_budget(service, tmp_path):
    paths = []
    for index in range(20):
        path = tmp_path / f"bulk_{index}.py"
        path.write_text("\n".join(f"value_{line} = '{index}-{line}'" for line in range(50)), encoding="utf-8")
        paths.append(path.name)
    result = service.execute(":read-many " + " ".join(paths) + " --count 100 --max-bytes 262144")
    assert result.ok
    assert result.data["requested"] == 20
    assert result.data["count"] == 20
    assert result.data["raw_bytes"] <= result.data["byte_budget"]


def test_structured_expected_hash_rejects_stale_patch(service, tmp_path):
    current = service.files.hash("sample.py")
    good = service._execute_structured({
        "op": "operation", "type": "replace_line", "path": "sample.py",
        "line": 1, "content": "NumeroProtocolo = 2", "expected_hash": current,
    })
    assert good.ok
    stale = service._execute_structured({
        "op": "operation", "type": "replace_line", "path": "sample.py",
        "line": 1, "content": "NumeroProtocolo = 3", "expected_hash": current,
    })
    assert not stale.ok
    assert stale.data["results"][0]["code"] == "STALE_FILE"



def test_service_close_joins_background_indexer(tmp_path):
    for index in range(80):
        (tmp_path / f"warm_{index}.py").write_text(f"value = {index}\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    service.close()
    thread = service.indexer._build_thread
    assert thread is None or not thread.is_alive()



def test_runtime_service_detects_dotnet_api_worker_and_test_profiles(tmp_path):
    from easychange.core.process_service import ProcessService
    from easychange.core.runtime_service import ProjectRuntimeService

    api = tmp_path / "Api"; api.mkdir()
    (api / "Api.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>',
        encoding="utf-8",
    )
    (api / "Program.cs").write_text(
        "var app = WebApplication.CreateBuilder(args).Build(); app.MapControllers();",
        encoding="utf-8",
    )
    props = api / "Properties"; props.mkdir()
    (props / "launchSettings.json").write_text(
        '{"profiles":{"http":{"applicationUrl":"http://localhost:5123;https://localhost:7123"}}}',
        encoding="utf-8",
    )

    worker = tmp_path / "Worker"; worker.mkdir()
    (worker / "Worker.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk.Worker"><PropertyGroup><OutputType>Exe</OutputType></PropertyGroup></Project>',
        encoding="utf-8",
    )
    (worker / "Worker.cs").write_text("public class Worker : BackgroundService {}", encoding="utf-8")

    tests = tmp_path / "Tests"; tests.mkdir()
    (tests / "Tests.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><IsTestProject>true</IsTestProject></PropertyGroup></Project>',
        encoding="utf-8",
    )

    service = ProjectRuntimeService(Workspace.open(tmp_path), ProcessService(tmp_path))
    profiles = service.profiles()
    kinds = {item["kind"] for item in profiles}
    assert {"dotnet-api", "dotnet-worker", "dotnet-test"} <= kinds
    api_profile = next(item for item in profiles if item["kind"] == "dotnet-api")
    assert "http://localhost:5123/swagger/index.html" in api_profile["swagger_candidates"]


def test_database_service_sqlite_read_only_and_schema(tmp_path):
    import sqlite3
    from easychange.core.database_service import DatabaseService

    database = tmp_path / "sample.db"
    connection = sqlite3.connect(database)
    connection.execute("create table people(id integer primary key, name text)")
    connection.execute("insert into people(name) values ('Ada'),('Linus')")
    connection.commit(); connection.close()

    service = DatabaseService(Workspace.open(tmp_path))
    result = service.query("sample.db", "select id,name from people order by id")
    assert result["row_count"] == 2
    assert result["rows"][0][1] == "Ada"
    schema = service.schema("sample.db")
    assert any(row[0] == "people" for row in schema["rows"])
    with pytest.raises(PermissionError):
        service.query("sample.db", "delete from people")


def test_runtime_evidence_creates_local_report(tmp_path):
    from easychange.core.process_service import ProcessService
    from easychange.core.runtime_service import ProjectRuntimeService

    service = ProjectRuntimeService(Workspace.open(tmp_path), ProcessService(tmp_path))
    evidence = service.evidence("unit-test")
    assert (tmp_path / evidence["evidence_json"]).exists()
    assert (tmp_path / evidence["report"]).exists()


def test_command_service_exposes_runtime_and_database_capabilities(tmp_path):
    import sqlite3

    database = tmp_path / "sample.db"
    connection = sqlite3.connect(database)
    connection.execute("create table items(id integer)")
    connection.execute("insert into items(id) values (1)")
    connection.commit(); connection.close()

    service = CommandService(Workspace.open(tmp_path))
    try:
        capabilities = service.execute(":capabilities")
        assert capabilities.ok
        assert capabilities.data["capabilities"]["runtime"]["smart_test"] is True
        assert capabilities.data["capabilities"]["database"]["read_only_default"] is True
        query = service.execute(":db-query sample.db select id from items")
        assert query.ok
        assert query.data["rows"] == [[1]]
        evidence = service.execute(":evidence --title smoke")
        assert evidence.ok
        assert (tmp_path / evidence.data["report"]).exists()
    finally:
        service.close()



def test_database_configure_persists_alias_without_secret_values(tmp_path):
    from easychange.core.database_service import DatabaseService

    service = DatabaseService(Workspace.open(tmp_path))
    result = service.configure(
        "local-sql", "sqlserver",
        {"server_env": "DB_SERVER", "database_env": "DB_NAME",
         "user_env": "DB_USER", "password_env": "DB_PASSWORD"},
    )
    assert result["connection"]["provider"] == "sqlserver"
    assert service.connections()["connections"]["local-sql"]["password_env"] == "DB_PASSWORD"
    with pytest.raises(ValueError):
        service.configure("bad", "sqlserver", {"password": "secret"})



def test_read_many_parallel_bulk_64_files(service, tmp_path):
    paths = []
    for index in range(64):
        path = tmp_path / f"parallel_{index}.py"
        path.write_text("\n".join(f"value_{line} = {line}" for line in range(40)), encoding="utf-8")
        paths.append(path.name)
    result = service.execute(
        ":read-many " + " ".join(paths) + " --count 80 --max-bytes 8388608"
    )
    assert result.ok
    assert result.data["count"] == 64
    assert result.data["parallel_workers"] > 1
    assert result.data["requested"] == 64


def test_custom_runtime_profile_supports_arbitrary_project_type(tmp_path):
    from easychange.core.process_service import ProcessService
    from easychange.core.runtime_service import ProjectRuntimeService

    runtime = ProjectRuntimeService(Workspace.open(tmp_path), ProcessService(tmp_path))
    configured = runtime.configure_profile(
        "cron-nightly",
        kind="cron",
        run_argv=["python", "cron.py"],
        test_argv=["python", "-m", "pytest", "-q"],
        urls=[],
    )
    assert configured["profile"]["kind"] == "cron"
    profile = runtime.profile("cron-nightly")
    assert profile["run_argv"] == ["python", "cron.py"]
    assert profile["test_argv"] == ["python", "-m", "pytest", "-q"]
    assert profile["configured"] is True


def test_runtime_dotnet_no_restore_only_after_assets_exist(tmp_path):
    from easychange.core.process_service import ProcessService
    from easychange.core.runtime_service import ProjectRuntimeService

    project = tmp_path / "App"; project.mkdir()
    (project / "App.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>',
        encoding="utf-8",
    )
    (project / "Program.cs").write_text("var app = WebApplication.CreateBuilder(args).Build();", encoding="utf-8")
    runtime = ProjectRuntimeService(Workspace.open(tmp_path), ProcessService(tmp_path))
    first = next(item for item in runtime.profiles() if item["project"] == "App/App.csproj")
    assert "--no-restore" not in first["run_argv"]

    obj = project / "obj"; obj.mkdir()
    (obj / "project.assets.json").write_text("{}", encoding="utf-8")
    second = next(item for item in runtime.profiles() if item["project"] == "App/App.csproj")
    assert "--no-restore" in second["run_argv"]



def test_database_read_only_classifier_rejects_mutating_cte_and_select_into():
    from easychange.core.database_service import DatabaseService

    assert DatabaseService._is_read_only("SELECT 'delete' AS word") is True
    assert DatabaseService._is_read_only("WITH x AS (SELECT 1) SELECT * FROM x") is True
    assert DatabaseService._is_read_only("WITH x AS (SELECT 1) DELETE FROM target") is False
    assert DatabaseService._is_read_only("SELECT * INTO backup_table FROM source_table") is False
    assert DatabaseService._is_read_only("EXPLAIN SELECT * FROM source_table") is True
    assert DatabaseService._is_read_only("EXPLAIN UPDATE target SET x=1") is False
    assert DatabaseService._is_read_only("PRAGMA table_info(users)") is True
    assert DatabaseService._is_read_only("PRAGMA journal_mode=WAL") is False



def test_structured_result_has_stable_schema_and_primary(service):
    result = service._execute_structured({
        "op": "operation",
        "type": "read",
        "path": "sample.py",
        "start": 1,
        "count": 1,
    })
    assert result.ok
    assert result.command == "structured"
    assert result.data["schema"] == "easychange.structured/2"
    assert result.data["operation"] == "operation"
    assert result.data["count"] == 1
    assert result.data["primary"] == result.data["results"][0]
    assert result.data["primary"]["command"] == "read"



def test_git_watcher_uses_one_status_process_when_head_is_stable(tmp_path):
    from easychange.core.indexer import Indexer
    from easychange.core.watcher import WorkspaceWatcher

    indexer = Indexer(tmp_path)
    watcher = WorkspaceWatcher(indexer)
    calls = []

    def fake_git(argv):
        calls.append(list(argv))
        return b"# branch.oid abc123\0# branch.head main\0"

    watcher._git = fake_git
    first = watcher._poll_git()
    second = watcher._poll_git()

    assert first["backend"] == "git-incremental"
    assert first["status_backend"] == "porcelain-v2"
    assert second["backend"] == "git-incremental"
    assert calls == [
        ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"],
        ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"],
    ]


def test_process_run_uses_bounded_tempfile_capture(service):
    import sys

    result = service.processes.run([
        sys.executable,
        "-c",
        "import sys; print('x' * 200000); print('ERROR CS9999: bounded capture', file=sys.stderr)",
    ])
    assert result["capture_backend"] == "tempfile-tail"
    assert result["stdout_chars"] >= 200000
    assert len(result["stdout"]) <= 4000
    assert "CS9999" in result["diagnostics"]
    assert not list((service.workspace.root_path / ".easychange" / "processes").glob("run-*.out"))
    assert not list((service.workspace.root_path / ".easychange" / "processes").glob("run-*.err"))



def test_compact_state_omits_expensive_detail(service):
    result = service.execute(":state --compact")
    assert result.ok
    assert result.data["state"] == "READY"
    assert "workspace" in result.data
    assert "instance_id" in result.data
    assert "git" not in result.data
    assert "process" not in result.data
    assert "index_details" not in result.data



def test_git_status_uses_single_process_and_preserves_shape(tmp_path, monkeypatch):
    from easychange.core.git_service import GitService

    service = GitService(Workspace.open(tmp_path))
    calls = []

    def fake_run(*args):
        calls.append(args)
        return "## main...origin/main [ahead 1]\n M src/Foo.cs\n?? new.txt"

    monkeypatch.setattr(service, "_run", fake_run)
    result = service.status()
    assert calls == [("status", "--short", "--branch")]
    assert result == {
        "branch": "main",
        "changes": [" M src/Foo.cs", "?? new.txt"],
        "clean": False,
    }


def test_smart_test_reuses_one_runtime_scan(tmp_path, monkeypatch):
    from easychange.core.process_service import ProcessService
    from easychange.core.runtime_service import ProjectRuntimeService

    service = ProjectRuntimeService(Workspace.open(tmp_path), ProcessService(tmp_path))
    calls = {"profiles": 0}

    profile = {
        "id": "custom:test", "ecosystem": "custom", "kind": "custom-test",
        "project": "runtime_profiles.json", "runnable": False, "run_argv": None,
        "test_argv": ["python", "-c", "print('ok')"], "urls": [], "swagger_candidates": [],
    }

    def fake_profiles():
        calls["profiles"] += 1
        return [dict(profile)]

    monkeypatch.setattr(service, "profiles", fake_profiles)
    result = service.smart_test("custom:test", timeout=30)
    assert result["passed"] is True
    assert calls["profiles"] == 1



def test_study_keeps_primary_results_when_references_fail(service, monkeypatch):
    monkeypatch.setattr(
        service.symbol_service,
        "references",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AttributeError("'NoneType' object has no attribute 'splitlines'")
        ),
    )
    result = service.execute(":study NumeroProtocolo --limit 4 --context 1")
    assert result.ok
    assert result.data["matches"]
    assert result.data["complete"] is False
    assert result.data["references"] == []
    assert any(item["stage"] == "references" for item in result.data["warnings"])


def test_git_search_tolerates_none_stdout(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from easychange.core.indexer import Indexer

    (tmp_path / ".git").mkdir()
    indexer = Indexer(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=None, stderr=None),
    )
    assert indexer._git_search(
        "Anything", regex=False, extension=None, path_prefix=None, case_sensitive=False
    ) == []



def test_background_process_rejects_duplicate_running_id(tmp_path):
    import sys
    from easychange.core.process_service import ProcessService

    service = ProcessService(tmp_path)
    started = service.start(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        "P1",
    )
    assert started["state"] == "RUNNING"
    try:
        with pytest.raises(RuntimeError, match="already running"):
            service.start(
                [sys.executable, "-c", "print('replacement')"],
                "P1",
            )
    finally:
        service.stop("P1")


def test_database_cli_query_uses_bounded_tempfile_capture(monkeypatch):
    import os
    import sys
    from easychange.core.database_service import DatabaseService

    argv = [
        sys.executable,
        "-c",
        "import sys; [print('row-%d' % i) for i in range(1000)]",
    ]
    result = DatabaseService._cli_query(
        "test", argv, dict(os.environ), max_rows=10, timeout=30
    )
    assert result["row_count"] == 10
    assert result["truncated"] is True
    assert result["capture_backend"] == "tempfile-bounded"
    assert result["rows"][0] == "row-0"



def test_read_only_structured_batch_persists_once(service, monkeypatch):
    saves = {"count": 0}
    original = service.state_store.save

    def counted_save():
        saves["count"] += 1
        return original()

    monkeypatch.setattr(service.state_store, "save", counted_save)
    result = service._execute_structured({
        "op": "batch",
        "operations": [
            {"type": "read", "path": "sample.py", "start": 1, "count": 5},
            {"type": "search", "query": "NumeroProtocolo"},
            {"type": "read", "path": "sample.py", "start": 1, "count": 5},
        ],
    })
    assert result.ok
    assert saves["count"] == 1


def test_sequenced_read_only_structured_batch_persists_running_and_done_only(service, monkeypatch):
    import json

    saves = {"count": 0}
    original = service.state_store.save

    def counted_save():
        saves["count"] += 1
        return original()

    monkeypatch.setattr(service.state_store, "save", counted_save)
    packet = json.dumps({
        "op": "batch",
        "operations": [
            {"type": "read", "path": "sample.py", "start": 1, "count": 5},
            {"type": "search", "query": "NumeroProtocolo"},
            {"type": "read", "path": "sample.py", "start": 1, "count": 5},
        ],
    }, separators=(",", ":"))
    result = service.execute(":ec QPERSIST01 :j1 " + packet)
    assert result.ok
    # One durable RUNNING journal write + one durable DONE write. No per-read saves.
    assert saves["count"] == 2


def test_execute_tokens_error_persists_once(service, monkeypatch):
    saves = {"count": 0}
    original = service.state_store.save

    def counted_save():
        saves["count"] += 1
        return original()

    monkeypatch.setattr(service.state_store, "save", counted_save)
    result = service.execute_tokens("read", ["missing-file.py"])
    assert not result.ok
    assert saves["count"] == 1



def test_study_context_is_not_persisted_in_result_navigation_cache(service):
    result = service.execute(":study NumeroProtocolo --limit 4 --context 3")
    assert result.ok
    assert result.data["matches"]
    match = result.data["matches"][0]
    assert "context" in match
    cached = service.results[match["id"]]
    assert cached["file"] == match["file"]
    assert cached["line"] == match["line"]
    assert "context" not in cached


def test_result_reference_resolves_without_file_ref_duplicate(service):
    result = service.execute(":search NumeroProtocolo --limit 2")
    match = result.data["matches"][0]
    service.file_refs.pop(match["id"], None)
    assert service._resolve_ref(match["id"]) == match["file"]


def test_file_refs_are_bounded_to_1024(service):
    service.file_refs = {f"F{i}": f"file-{i}.txt" for i in range(1400)}
    service._persist_state()
    assert len(service.state_store.data["file_refs"]) == 1024



def test_process_history_prunes_old_finished_entries_and_logs(tmp_path):
    from types import SimpleNamespace
    from easychange.core.process_service import ProcessService, RunningProcess

    service = ProcessService(tmp_path)
    for index in range(40):
        stdout_path = service.root / f"P{index}.out"
        stderr_path = service.root / f"P{index}.err"
        stdout_path.write_text("out", encoding="utf-8")
        stderr_path.write_text("err", encoding="utf-8")
        process = SimpleNamespace(poll=lambda: 0, pid=index)
        service._processes[f"P{index}"] = RunningProcess(
            f"P{index}", ["fake"], process, stdout_path, stderr_path
        )

    service._prune_finished(keep=32)
    assert len(service._processes) == 32
    assert "P0" not in service._processes
    assert not (service.root / "P0.out").exists()
    assert "P39" in service._processes
    assert (service.root / "P39.out").exists()


def test_stop_all_removes_ephemeral_process_logs(tmp_path):
    import sys
    from easychange.core.process_service import ProcessService

    service = ProcessService(tmp_path)
    service.start([sys.executable, "-c", "print('done')"], "P1")
    service._processes["P1"].process.wait(timeout=10)
    assert (service.root / "P1.out").exists()
    service.stop_all()
    assert service._processes == {}
    assert not (service.root / "P1.out").exists()
    assert not (service.root / "P1.err").exists()



def test_file_write_reads_existing_file_only_once(tmp_path, monkeypatch):
    from easychange.core.file_service import FileService

    target = tmp_path / "one.txt"
    target.write_text("before\n", encoding="utf-8")
    service = FileService(Workspace.open(tmp_path))
    service.remember("one.txt", replace=True)

    original = Path.read_bytes
    calls = {"target": 0}

    def counted_read_bytes(path):
        if path == target:
            calls["target"] += 1
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    result = service.write("one.txt", "after\n")
    assert result["created"] is False
    # One baseline read plus one mandatory post-write hash verification read.
    assert calls["target"] == 2
    assert result["verified"] is True
    assert target.read_text(encoding="utf-8") == "after\n"


def test_file_read_hashes_once_and_sets_full_baseline(tmp_path, monkeypatch):
    import hashlib
    from easychange.core.file_service import FileService

    target = tmp_path / "read.txt"
    target.write_text("hello\n", encoding="utf-8")
    service = FileService(Workspace.open(tmp_path))
    raw = target.read_bytes()
    result = service.read("read.txt")
    full = hashlib.sha256(raw).hexdigest()
    assert result["hash"] == full[:12]
    assert service.baselines["read.txt"] == full



def test_transaction_storage_is_append_only_and_restart_safe(tmp_path):
    import json
    source = tmp_path / "sample.txt"
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))

    assert service.execute(":begin").ok
    assert service.execute(":replace-line sample.txt 1 ONE").ok
    first_size = service.transaction_log_path.stat().st_size
    assert service.execute(":replace-line sample.txt 2 TWO").ok
    second_size = service.transaction_log_path.stat().st_size

    header = json.loads(service.transaction_path.read_text(encoding="utf-8"))
    assert header["format"] == "append-v2"
    assert second_size > first_size > 0
    assert len(service.transaction_log_path.read_text(encoding="utf-8").splitlines()) == 2

    restarted = CommandService(Workspace.open(tmp_path))
    assert restarted.transaction is not None
    assert len(restarted.transaction) == 2
    rollback = restarted.execute(":rollback")
    assert rollback.ok
    assert source.read_text(encoding="utf-8").replace("\r\n", "\n") == "one\ntwo\nthree\n"
    restarted.close()
    service.close()


def test_legacy_transaction_file_is_loaded_and_migrated(tmp_path):
    import json
    source = tmp_path / "legacy.txt"
    source.write_text("after\n", encoding="utf-8")
    easy = tmp_path / ".easychange"
    easy.mkdir()
    old = {
        "transaction_id": "TXLEGACY",
        "changes": [{
            "path": "legacy.txt",
            "before": "before\n",
            "after": "after\n",
            "change_id": None,
            "command": "replace-line",
        }],
    }
    (easy / "active_transaction.json").write_text(json.dumps(old), encoding="utf-8")

    service = CommandService(Workspace.open(tmp_path))
    assert service.transaction_id == "TXLEGACY"
    assert len(service.transaction or []) == 1
    # Append one more change to force migration.
    service.last_command = "replace-line"
    service._record("legacy.txt", "after\n", "after2\n")
    header = json.loads(service.transaction_path.read_text(encoding="utf-8"))
    assert header["format"] == "append-v2"
    assert len(service.transaction_log_path.read_text(encoding="utf-8").splitlines()) == 2
    service.close()


def test_structured_mutation_batch_avoids_per_edit_session_saves(tmp_path, monkeypatch):
    source = tmp_path / "sample.txt"
    source.write_text("\n".join(f"line {i}" for i in range(1, 101)) + "\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    saves = {"count": 0}
    original = service.state_store.save

    def counted_save():
        saves["count"] += 1
        return original()

    monkeypatch.setattr(service.state_store, "save", counted_save)
    operations = [
        {"type": "replace_line", "path": "sample.txt", "line": i,
         "content": f"changed {i}"}
        for i in range(1, 21)
    ]
    result = service._execute_structured({"op": "batch", "operations": operations})
    assert result.ok
    # begin + commit + final structured result; independent of edit count.
    assert saves["count"] == 3



def test_replace_line_reuses_edit_snapshots_without_post_read(tmp_path, monkeypatch):
    source = tmp_path / "sample.txt"
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    service.indexer.close()

    original_read_bytes = Path.read_bytes
    reads = {"source": 0}

    def counted_read_bytes(path):
        if path == source:
            reads["source"] += 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    result = service.execute(":replace-line sample.txt 2 TWO")
    assert result.ok
    # snapshot + baseline check + mandatory post-write verification + index refresh.
    assert reads["source"] <= 4
    assert source.read_text(encoding="utf-8").replace("\r\n", "\n") == "one\nTWO\nthree\n"


def test_file_edit_rejects_stale_existing_baseline(tmp_path):
    from easychange.core.file_service import ExternalChangeError, FileService

    source = tmp_path / "stale.txt"
    source.write_text("old\n", encoding="utf-8")
    files = FileService(Workspace.open(tmp_path))
    files.read("stale.txt")
    source.write_text("external\n", encoding="utf-8")
    with pytest.raises(ExternalChangeError):
        files.replace_line("stale.txt", 1, "mine")



def test_transaction_defers_reindex_until_commit(tmp_path, monkeypatch):
    source = tmp_path / "sample.txt"
    source.write_text("\n".join(f"line {i}" for i in range(1, 51)) + "\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    service.indexer.close()

    calls = []
    original = service.indexer.update_path

    def counted(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(service.indexer, "update_path", counted)
    assert service.execute(":begin").ok
    for line in range(1, 11):
        assert service.execute(f":replace-line sample.txt {line} changed-{line}").ok
    assert calls == []

    committed = service.execute(":commit")
    assert committed.ok
    assert committed.data["indexed_paths"] == 1
    assert calls == ["sample.txt"]


def test_transaction_search_flushes_pending_index_for_read_after_write(tmp_path, monkeypatch):
    source = tmp_path / "sample.txt"
    source.write_text("before\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    service.indexer.close()
    service.indexer.refresh(force=True)

    calls = []
    original = service.indexer.update_path

    def counted(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(service.indexer, "update_path", counted)
    assert service.execute(":begin").ok
    assert service.execute(":replace-line sample.txt 1 UNIQUE_AFTER_WRITE").ok
    assert calls == []

    found = service.execute(":search UNIQUE_AFTER_WRITE")
    assert found.ok
    assert found.data["matches"]
    assert calls == ["sample.txt"]

    committed = service.execute(":commit")
    assert committed.ok
    assert committed.data["indexed_paths"] == 0



def test_patch_set_uses_original_snapshot_coordinates_when_range_changes_line_count(tmp_path):
    path = tmp_path / "multi.txt"
    path.write_text("".join(f"L{i}\n" for i in range(1, 121)), encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    payload = {
        "op": "batch",
        "operations": [
            {"type": "replace_range", "path": "multi.txt", "start": 55, "end": 58,
             "content": "\n".join(f"R{i}" for i in range(1, 11))},
            {"type": "replace_line", "path": "multi.txt", "line": 99, "content": "LINE99_CHANGED"},
        ],
    }
    result = service.execute(":ec QSAFEA1 :j1 " + json.dumps(payload))
    assert result.ok, result.to_dict()
    final = path.read_text(encoding="utf-8")
    assert "LINE99_CHANGED\n" in final
    assert "L99\n" not in final
    assert "L93\n" in final
    receipt = result.data["mutation_receipt"]
    assert receipt["state"] == "COMMITTED"
    assert receipt["verified"] is True
    assert receipt["changed_files"] == 1


def test_sequenced_write_receipt_survives_restart_and_replay_does_not_write_twice(tmp_path):
    path = tmp_path / "receipt.txt"
    path.write_text("before\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    payload = {"op": "operation", "type": "write_file", "path": "receipt.txt", "content": "after\n"}
    command = ":ec QSAFEB1 :j1 " + json.dumps(payload)
    first = service.execute(command)
    assert first.ok
    first_receipt = first.data["mutation_receipt"]
    assert first_receipt["state"] == "COMMITTED"
    assert first_receipt["verified"] is True
    assert path.read_text(encoding="utf-8") == "after\n"
    service.close()

    replacement = CommandService(Workspace.open(tmp_path))
    def fail_if_reexecuted(*args, **kwargs):
        raise AssertionError("write replayed instead of using durable receipt")
    replacement.files.write = fail_if_reexecuted
    replay = replacement.execute(command)
    assert replay.ok
    assert replay.data["duplicate"] is True
    assert replay.data["mutation_receipt"] == first_receipt
    assert path.read_text(encoding="utf-8") == "after\n"


def test_same_sequence_structured_append_is_idempotent(tmp_path):
    path = tmp_path / "once.txt"
    path.write_text("base\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    payload = {"op": "operation", "type": "append", "path": "once.txt", "content": "ONLY_ONCE"}
    command = ":ec QSAFEI1 :j1 " + json.dumps(payload)
    first = service.execute(command)
    second = service.execute(command)
    assert first.ok and second.ok
    assert second.data["duplicate"] is True
    assert path.read_text(encoding="utf-8").count("ONLY_ONCE") == 1


def test_patch_set_rejects_overlapping_original_snapshot_edits_before_write(tmp_path):
    path = tmp_path / "overlap.txt"
    original = "".join(f"L{i}\n" for i in range(1, 31))
    path.write_text(original, encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    payload = {
        "op": "batch",
        "operations": [
            {"type": "replace_range", "path": "overlap.txt", "start": 10, "end": 20, "content": "BLOCK"},
            {"type": "replace_line", "path": "overlap.txt", "line": 15, "content": "CONFLICT"},
        ],
    }
    result = service.execute(":ec QSAFEJ1 :j1 " + json.dumps(payload))
    assert not result.ok
    assert result.code == "PATCH_OVERLAP"
    assert result.data["transaction"]["state"] == "NOT_STARTED"
    assert path.read_text(encoding="utf-8") == original


def test_durable_result_store_exposes_individually_verified_chunks(tmp_path):
    from easychange.core.result_store import DurableResultStore
    import base64
    import binascii

    store = DurableResultStore(tmp_path / "results")
    sequence = "QSAFEC1"
    result = {"ok": True, "data": {"text": "x" * 6000}}
    digest = hashlib.sha256(b"command").hexdigest()
    store.mark_running(sequence, digest, "command", mutation=False)
    store.complete(sequence, digest, "command", result)
    meta = store.chunk_meta(sequence, chunk_bytes=512)
    assert meta and meta["total_chunks"] >= 12

    rebuilt = bytearray()
    for index in range(meta["total_chunks"]):
        chunk = store.chunk(sequence, index, chunk_bytes=512)
        assert chunk is not None
        payload = base64.urlsafe_b64decode(chunk["payload_b64"] + "=" * (-len(chunk["payload_b64"]) % 4))
        assert f"{binascii.crc32(payload) & 0xFFFFFFFF:08x}" == chunk["crc32"]
        rebuilt.extend(payload)
    assert hashlib.sha256(bytes(rebuilt)).hexdigest() == meta["result_hash"]

    missing = store.chunk(sequence, 5, chunk_bytes=512)
    assert missing["chunk_index"] == 5



def test_long_running_job_result_survives_missed_terminal_frame(tmp_path):
    import sys
    import time as _time

    service = CommandService(Workspace.open(tmp_path))
    job_id = "JLONG1"
    start = service._execute_structured({
        "op": "operation",
        "type": "job_start",
        "job_id": job_id,
        "argv": [sys.executable, "-c", "import time; print('BEGIN'); time.sleep(0.15); print('BUILD_OK')"],
    })
    assert start.ok
    assert start.data["state"] == "RUNNING"

    deadline = _time.time() + 5
    status = None
    while _time.time() < deadline:
        status = service._execute_structured({
            "op": "operation", "type": "job_status", "job_id": job_id,
        })
        assert status.ok
        if status.data["state"] != "RUNNING":
            break
        _time.sleep(0.03)

    assert status is not None
    assert status.data["state"] == "SUCCEEDED"

    # Simulate the visual consumer missing the exact completion frame: query
    # terminal output later through a separate operation.
    result = service._execute_structured({
        "op": "operation", "type": "job_result", "job_id": job_id,
    })
    assert result.ok
    assert result.data["state"] == "SUCCEEDED"
    assert result.data["returncode"] == 0
    assert "BEGIN" in result.data["stdout"]
    assert "BUILD_OK" in result.data["stdout"]
    assert result.data["result_hash"]


def test_job_start_is_idempotent_by_job_id_and_conflicting_argv_is_rejected(tmp_path):
    import sys

    service = CommandService(Workspace.open(tmp_path))
    argv = [sys.executable, "-c", "import time; time.sleep(0.2)"]
    first = service._execute_structured({
        "op": "operation", "type": "job_start", "job_id": "JIDEMP1", "argv": argv,
    })
    second = service._execute_structured({
        "op": "operation", "type": "job_start", "job_id": "JIDEMP1", "argv": argv,
    })
    assert first.ok and second.ok
    assert first.data["pid"] == second.data["pid"]

    conflict = service._execute_structured({
        "op": "operation", "type": "job_start", "job_id": "JIDEMP1",
        "argv": [sys.executable, "-c", "print('different')"],
    })
    assert not conflict.ok
    assert "JOB_ID_CONFLICT" in (conflict.error or "")
    service.processes.job_cancel("JIDEMP1")


def test_discover_many_returns_definitions_references_and_impact(service, tmp_path):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "Feature.cs").write_text(
        "public sealed class Feature { public int NumeroProtocolo; }\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "Caller.cs").write_text(
        "public sealed class Caller { Feature value = new Feature(); int x = NumeroProtocolo; }\n",
        encoding="utf-8",
    )
    service.indexer.invalidate()

    result = service._execute_structured({
        "op": "operation",
        "type": "discover_many",
        "terms": ["Feature", "NumeroProtocolo"],
        "limit": 8,
        "definitions": 4,
        "references": 8,
    })

    assert result.ok
    data = result.data["primary"]["data"] if "primary" in result.data else result.data["results"][0]["data"]
    assert data["terms"] == ["Feature", "NumeroProtocolo"]
    assert data["by_term"]["Feature"]["definitions"]
    assert data["by_term"]["Feature"]["references"]
    assert data["by_term"]["NumeroProtocolo"]["references"]
    assert any(item["file"].endswith("Caller.cs") for item in data["impact_files"])
    assert data["duration_ms"] >= 0


def test_discover_many_cli_preserves_existing_search_tools(service, tmp_path):
    (tmp_path / "Alpha.cs").write_text(
        "public sealed class Alpha { public void Run() {} }\n",
        encoding="utf-8",
    )
    (tmp_path / "Use.cs").write_text(
        "public sealed class Use { Alpha item = new Alpha(); }\n",
        encoding="utf-8",
    )
    service.indexer.invalidate()

    discovered = service.execute(":discover-many Alpha --limit 6 --definitions 4 --references 6")
    searched = service.execute(":search Alpha")

    assert discovered.ok
    assert discovered.data["by_term"]["Alpha"]["definitions"]
    assert discovered.data["by_term"]["Alpha"]["references"]
    assert searched.ok and searched.data["matches"]


def test_cold_git_discovery_uses_one_multi_pattern_process(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from easychange.core.indexer import Indexer

    (tmp_path / ".git").mkdir()
    (tmp_path / "A.cs").write_text(
        "public sealed class Alpha { Beta value; }\n",
        encoding="utf-8",
    )
    (tmp_path / "B.cs").write_text(
        "public sealed class Beta { Alpha value; }\n",
        encoding="utf-8",
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "A.cs:1:public sealed class Alpha { Beta value; }\n"
                "B.cs:1:public sealed class Beta { Alpha value; }\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    indexer = Indexer(tmp_path)
    try:
        result = indexer.discover_many(["Alpha", "Beta"], references_per_term=8)
        assert result["engine"] == "git-grep"
        assert result["single_pass"] is True
        assert len(calls) == 1
        assert calls[0].count("-e") == 2
        assert "Alpha" in calls[0] and "Beta" in calls[0]
        assert result["by_term"]["Alpha"]["definitions"]
        assert result["by_term"]["Beta"]["definitions"]
    finally:
        indexer.close()


def test_read_regions_coalesces_overlaps_and_reads_in_parallel(service, tmp_path):
    (tmp_path / "A.cs").write_text(
        "\n".join(f"line {i}" for i in range(1, 121)) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "B.cs").write_text(
        "\n".join(f"other {i}" for i in range(1, 81)) + "\n",
        encoding="utf-8",
    )
    result = service._execute_structured({
        "op": "operation",
        "type": "read_regions",
        "regions": [
            {"path": "A.cs", "start": 10, "count": 12},
            {"path": "A.cs", "start": 18, "count": 10},
            {"path": "B.cs", "start": 30, "count": 8},
        ],
        "max_bytes": 262144,
        "merge_overlaps": True,
    })
    assert result.ok
    data = result.data["primary"]["data"]
    assert data["requested"] == 3
    assert data["coalesced"] == 2
    assert data["count"] == 2
    assert data["parallel_workers"] == 2
    a = next(item for item in data["regions"] if item["path"].endswith("A.cs"))
    assert a["start"] == 10
    assert a["returned_count"] >= 18


def test_discover_many_can_attach_context_pack_in_same_operation(service, tmp_path):
    (tmp_path / "src").mkdir(exist_ok=True)
    lines = [
        "public sealed class Feature",
        "{",
        "    public int NumeroProtocolo;",
        "    public void Run() { NumeroProtocolo++; }",
        "}",
        "public sealed class Caller",
        "{",
        "    Feature value = new Feature();",
        "}",
    ]
    (tmp_path / "src" / "Feature.cs").write_text("\n".join(lines) + "\n", encoding="utf-8")
    service.indexer.invalidate()
    result = service._execute_structured({
        "op": "operation",
        "type": "discover_many",
        "terms": ["Feature", "NumeroProtocolo"],
        "references": 8,
        "context": 2,
        "context_files": 4,
        "hits_per_file": 2,
        "context_max_bytes": 262144,
    })
    assert result.ok
    data = result.data["primary"]["data"]
    assert data["context_pack"]["count"] >= 1
    joined = "\n".join(
        line
        for region in data["context_pack"]["regions"]
        for line in region["lines"]
    )
    assert "Feature" in joined
    assert "NumeroProtocolo" in joined


def test_file_read_cache_reuses_snapshot_and_invalidates_external_change(service, tmp_path):
    path = tmp_path / "cache.txt"
    path.write_text("\n".join(f"line-{i}" for i in range(200)) + "\n", encoding="utf-8")

    first = service.files.read("cache.txt", 1, 20)
    second = service.files.read("cache.txt", 40, 20)

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first["hash"] == second["hash"]

    path.write_text("externally changed\n", encoding="utf-8")
    changed = service.files.read("cache.txt", 1, 20)

    assert changed["cache_hit"] is False
    assert changed["lines"] == ["1|externally changed"]
    assert changed["hash"] != first["hash"]


def test_verified_write_refreshes_read_cache(service):
    service.files.read("sample.py", 1, 20)
    result = service.files.write("sample.py", "updated = True\n")
    assert result["verified"] is True

    read = service.files.read("sample.py", 1, 20)
    assert read["cache_hit"] is True
    assert read["lines"] == ["1|updated = True"]
