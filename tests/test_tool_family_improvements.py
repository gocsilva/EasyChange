from __future__ import annotations
import sqlite3
import sys
import time
from easychange.core.database_service import DatabaseService
from easychange.core.file_service import FileService
from easychange.core.process_service import ProcessService
from easychange.core.result_store import DurableResultStore
from easychange.core.workspace import Workspace

def test_file_read_exposes_continuation_metadata(tmp_path):
    path = tmp_path / "many.txt"
    path.write_text("\n".join(f"line-{i}" for i in range(1, 8)) + "\n", encoding="utf-8")
    files = FileService(Workspace.open(tmp_path), {})
    first = files.read("many.txt", 1, 3)
    assert first["returned_lines"] == 3 and first["has_more"] is True and first["next_start"] == 4
    second = files.read("many.txt", first["next_start"], 3)
    assert second["previous_start"] == 1

def test_durable_result_chunks_expose_continuation_without_contract_loss(tmp_path):
    store = DurableResultStore(tmp_path / "results")
    sequence = "QCHUNK1"
    result = {"ok": True, "command": "read", "data": {"payload": "x" * 3000}}
    store.mark_running(sequence, "a" * 64, ":read x", mutation=False)
    store.complete(sequence, "a" * 64, ":read x", result)
    first = store.chunk(sequence, 0, chunk_bytes=256)
    assert first["payload_bytes"] <= 256 and first["has_more"] is True and first["next_chunk_index"] == 1
    assert store.status(sequence)["duration_ms"] is not None

def test_process_logs_remain_available_from_durable_job_record(tmp_path):
    service = ProcessService(tmp_path)
    job = service.job_start([sys.executable, "-c", "print('durable-log')"], "JLOG1")
    deadline = time.time() + 10
    status = service.job_status(job["job_id"])
    while status["state"] == "RUNNING" and time.time() < deadline:
        time.sleep(0.02)
        status = service.job_status(job["job_id"])
    assert status["terminal"] is True
    service._processes.pop(job["job_id"], None)
    logs = service.logs(job["job_id"], limit=2048)
    assert "durable-log" in logs["stdout"] and logs["durable"] is True

def test_database_results_expose_consistent_limits_and_has_more(tmp_path):
    db_path = tmp_path / "sample.sqlite"
    with sqlite3.connect(db_path) as db:
        db.execute("create table sample(id integer)")
        db.executemany("insert into sample(id) values (?)", [(1,), (2,), (3,)])
    service = DatabaseService(Workspace.open(tmp_path))
    result = service.query("sample.sqlite", "select id from sample order by id", max_rows=2)
    assert result["row_count"] == 2 and result["truncated"] is True and result["has_more"] is True
    assert result["max_rows"] == 2 and result["read_only"] is True


def test_capabilities_publish_canonical_structured_operation_profiles(tmp_path):
    from easychange.core.command_service import CommandService
    service = CommandService(Workspace.open(tmp_path))
    capabilities = service.execute(":capabilities").data["capabilities"]["ai_machine"]["structured_operations"]
    assert capabilities["operations"]["write_file"]["mutation"] is True
    assert capabilities["operations"]["result_get"]["recovery"] is True
    assert "write" in capabilities["text_mutation_commands"]

def test_files_search_and_history_publish_direct_continuation(tmp_path):
    from easychange.core.command_service import CommandService
    for index in range(5):
        (tmp_path / f"f{index}.py").write_text(f"needle_{index} = 'needle'\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    files = service.execute(":files --limit 2").data
    assert files["returned"] == 2 and files["has_more"] is True and files["next_offset"] == 2
    search = service.execute(":search needle --limit 2").data
    assert search["returned"] == 2 and search["has_more"] is True and search["next_offset"] == 2
    history = service.execute(":history --limit 1").data
    assert history["returned"] == 1 and history["total"] >= 3

def test_tail_uses_bounded_read_and_preserves_continuation_metadata(tmp_path):
    from easychange.core.command_service import CommandService
    (tmp_path / "tail.txt").write_text("\n".join(str(i) for i in range(1, 51)) + "\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    result = service.execute(":tail tail.txt 1 5").data
    assert result["start"] == 46
    assert result["returned_lines"] == 5
    assert result["lines"][-1].endswith("|50")

def test_safe_structured_batch_can_continue_after_read_failure(tmp_path):
    from easychange.core.command_service import CommandService
    (tmp_path / "ok.txt").write_text("ok\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    result = service._execute_structured({
        "op": "batch",
        "continue_on_error": True,
        "operations": [
            {"type": "read", "path": "missing.txt"},
            {"type": "read", "path": "ok.txt"},
        ],
    })
    assert result.ok is False
    assert result.data["summary"] == {
        "requested": 2, "completed": 2, "succeeded": 1, "failed": 1,
        "skipped": 0, "continue_on_error": True,
    }
    assert result.data["results"][1]["ok"] is True
    assert result.data["results"][1]["command_id"]


def test_read_many_isolates_bad_path_and_keeps_valid_files(tmp_path):
    from easychange.core.command_service import CommandService
    (tmp_path / "ok.txt").write_text("ok\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    result = service.execute(":read-many missing.txt ok.txt")
    assert result.ok is True
    assert result.data["count"] == 1
    assert result.data["failed"] == 1
    assert result.data["errors"][0]["path"] == "missing.txt"
    assert result.data["partial"] is True
    assert result.data["files"][0]["path"] == "ok.txt"

def test_read_regions_isolates_bad_region_and_keeps_valid_regions(tmp_path):
    from easychange.core.command_service import CommandService
    (tmp_path / "ok.txt").write_text("one\ntwo\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    data = service._read_regions_data([
        {"path": "missing.txt", "start": 1, "count": 2},
        {"path": "ok.txt", "start": 1, "count": 2},
    ])
    assert data["count"] == 1
    assert data["failed"] == 1
    assert data["regions"][0]["path"] == "ok.txt"
    assert data["partial"] is True

def test_health_is_lightweight_additive_command(tmp_path):
    from easychange.core.command_service import CommandService
    service = CommandService(Workspace.open(tmp_path))
    health = service.execute(":health")
    assert health.ok is True
    assert health.data["state"] in {"READY", "DEGRADED"}
    assert health.data["durable_results"] is True
    assert "structured_operations" in health.data

def test_tail_accepts_additive_count_option_without_changing_legacy_positionals(tmp_path):
    from easychange.core.command_service import CommandService
    (tmp_path / "tail.txt").write_text("\n".join(str(i) for i in range(1, 21)) + "\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    option = service.execute(":tail tail.txt --count 3").data
    legacy = service.execute(":tail tail.txt 1 3").data
    assert option["lines"] == legacy["lines"]
    assert option["start"] == 18


def test_journal_compact_paginates_without_snapshot_payloads(tmp_path):
    from easychange.core.command_service import CommandService
    (tmp_path / "x.txt").write_text("a", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    service.execute(':write x.txt "b"')
    compact = service.execute(":journal --compact --limit 1").data
    assert compact["compact"] is True
    assert compact["returned"] == 1
    assert "before" not in compact["entries"][0]
    full = service.execute(":journal --limit 1").data
    assert full["compact"] is False
    assert full["entries"][0]["before_hash"] == compact["entries"][0]["before_hash"]

def test_git_metadata_and_bounded_diff_are_additive(tmp_path):
    import subprocess
    from easychange.core.command_service import CommandService
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "easychange@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "EasyChange"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.txt").write_text("one\n" + "changed\n" * 100, encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    status = service.execute(":status --summary").data
    assert status["change_count"] == len(status["changes"]) and status["clean"] is False
    bounded = service.execute(":diff a.txt --max-bytes 100").data
    assert bounded["diff_bytes"] >= bounded["returned_bytes"]
    assert bounded["returned_bytes"] <= 100
    assert bounded["truncated"] is True
    branch = service.execute(":branch --summary").data["branch"]
    assert branch["count"] == len(branch["branches"])

def test_process_run_reports_actual_byte_metadata(tmp_path):
    from easychange.core.process_service import ProcessService
    service = ProcessService(tmp_path)
    result = service.run([sys.executable, "-c", "print('abc')"])
    assert result["stdout_bytes"] == result["stdout_chars"]
    assert result["stderr_bytes"] == result["stderr_chars"]


def test_runtime_profiles_summary_is_additive(tmp_path):
    from easychange.core.command_service import CommandService
    (tmp_path / "pyproject.toml").write_text("[project]\nname='sample'\nversion='0.1'\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    result = service._execute_structured({"op": "operation", "type": "runtime_profiles"})
    assert result.ok is True
    profile_data = result.data["results"][0]["data"]
    assert profile_data["count"] == len(profile_data["profiles"])
    assert profile_data["runnable"] <= profile_data["count"]
    assert isinstance(profile_data["ecosystems"], dict)

def test_evidence_reports_generated_file_metadata(tmp_path, monkeypatch):
    from easychange.core.runtime_service import ProjectRuntimeService
    from easychange.core.process_service import ProcessService
    runtime = ProjectRuntimeService(Workspace.open(tmp_path), ProcessService(tmp_path))
    monkeypatch.setattr(runtime, "_browser_screenshot", lambda url, target: None)
    result = runtime.evidence("metadata")
    assert result["file_count"] >= 3
    assert result["total_bytes"] > 0
    assert len(result["files"]) == result["file_count"]
    assert result["duration_ms"] >= 0
