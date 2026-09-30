from pathlib import Path
import hashlib

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
    assert unknown.code == "UNKNOWN"
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
