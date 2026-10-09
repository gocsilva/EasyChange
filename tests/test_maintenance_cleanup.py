from __future__ import annotations

import json
import os
import time

from easychange.core.command_service import CommandService
from easychange.core.maintenance_service import MaintenanceService
from easychange.core.workspace import Workspace


def _old(path, seconds=10 * 24 * 60 * 60):
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_automatic_cleanup_prunes_only_easychange_owned_stale_artifacts(tmp_path):
    source = tmp_path / "src" / "Program.cs"
    source.parent.mkdir()
    source.write_text("class Program {}", encoding="utf-8")
    git_config = tmp_path / ".git" / "config"
    git_config.parent.mkdir()
    git_config.write_text("[core]\n", encoding="utf-8")
    unrelated_tmp = tmp_path / "user.tmp"
    unrelated_tmp.write_text("keep", encoding="utf-8")

    easy = tmp_path / ".easychange"
    results = easy / "results"
    jobs = easy / "jobs"
    processes = easy / "processes"
    evidence = easy / "evidence" / "old-evidence"
    blobs = easy / "blobs"
    for folder in (results, jobs, processes, evidence, blobs):
        folder.mkdir(parents=True, exist_ok=True)

    result_file = results / "QOLD1.json.zlib"
    result_file.write_bytes(b"old")
    _old(result_file)

    terminal_job = jobs / "JOLD.json"
    terminal_job.write_text(json.dumps({"state": "SUCCEEDED"}), encoding="utf-8")
    _old(terminal_job)

    stale_running = jobs / "JSTALE.json"
    stale_running.write_text(json.dumps({"state": "RUNNING"}), encoding="utf-8")
    _old(stale_running)

    active_running = jobs / "JACTIVE.json"
    active_running.write_text(json.dumps({"state": "RUNNING"}), encoding="utf-8")
    _old(active_running)

    stale_log = processes / "POLD.out"
    stale_log.write_text("old log", encoding="utf-8")
    _old(stale_log)

    active_log = processes / "PACTIVE.out"
    active_log.write_text("active log", encoding="utf-8")
    _old(active_log)

    evidence_file = evidence / "evidence.json"
    evidence_file.write_text("{}", encoding="utf-8")
    _old(evidence)
    _old(evidence_file)

    referenced = blobs / ("a" * 64 + ".zlib")
    referenced.write_bytes(b"referenced")
    orphan = blobs / ("b" * 64 + ".zlib")
    orphan.write_bytes(b"orphan")

    side_temp = source.with_name(f".{source.name}.easychange-123.tmp")
    side_temp.write_text("stale", encoding="utf-8")
    _old(side_temp)

    service = MaintenanceService(tmp_path)
    report = service.automatic_cleanup(
        referenced_blobs={"a" * 64},
        active_process_ids={"PACTIVE", "JACTIVE"},
        scan_workspace_temps=True,
    )

    assert source.read_text(encoding="utf-8") == "class Program {}"
    assert git_config.exists()
    assert unrelated_tmp.exists()
    # Even an unreadable legacy receipt is evidence that a sequence existed.
    assert result_file.exists()
    assert not terminal_job.exists()
    assert not stale_running.exists()
    assert active_running.exists()
    assert not stale_log.exists()
    assert active_log.exists()
    assert not evidence.exists()
    assert referenced.exists()
    assert not orphan.exists()
    assert not side_temp.exists()
    assert report["reclaimed_bytes"] > 0
    assert report["errors"] == []


def test_full_purge_removes_easychange_state_and_sidecar_temps_but_not_project(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("print('safe')\n", encoding="utf-8")
    git_keep = tmp_path / ".git" / "keep"
    git_keep.parent.mkdir()
    git_keep.write_text("safe", encoding="utf-8")
    other = tmp_path / "important.json"
    other.write_text('{"keep":true}', encoding="utf-8")

    easy = tmp_path / ".easychange"
    (easy / "results").mkdir(parents=True)
    (easy / "results" / "Q1.json.zlib").write_bytes(b"junk")
    side_temp = tmp_path / ".app.py.easychange-recovery.tmp"
    side_temp.write_text("junk", encoding="utf-8")

    report = MaintenanceService(tmp_path).purge_all()

    assert not easy.exists()
    assert not side_temp.exists()
    assert source.exists()
    assert git_keep.exists()
    assert other.exists()
    assert report["mode"] == "full"
    assert report["reclaimed_bytes"] > 0


def test_maintenance_refuses_non_easychange_paths(tmp_path):
    service = MaintenanceService(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("do not remove", encoding="utf-8")
    report = {
        "removed_files": 0,
        "reclaimed_bytes": 0,
        "categories": {},
        "removed": [],
        "errors": [],
    }
    try:
        service._remove(source, report, "bad", dry_run=False)
    except ValueError as exc:
        assert "REFUSING_NON_EASYCHANGE_PATH" in str(exc)
    else:
        raise AssertionError("non-EasyChange path was accepted")
    assert source.exists()


def test_health_and_maintenance_command_publish_storage_and_memory_bounds(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        health = service.execute(":health")
        assert health.ok is True
        assert "maintenance" in health.data
        assert "memory_bounds" in health.data["maintenance"]
        preview = service.execute(":maintenance --dry-run")
        assert preview.ok is True
        assert preview.data["dry_run"] is True
        assert preview.data["memory_bounds"]["history_limit"] == 100
    finally:
        service.close()


def test_human_cleanup_button_removes_easychange_data_and_preserves_project(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    import pytest
    pytest.importorskip("PySide6")
    pytest.importorskip("qrcode")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from easychange.ui.main_window import MainWindow

    source = tmp_path / "main.py"
    source.write_text("print('project stays')\n", encoding="utf-8")
    git_keep = tmp_path / ".git" / "config"
    git_keep.parent.mkdir()
    git_keep.write_text("[core]\n", encoding="utf-8")

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=False)
    window.show()
    app.processEvents()

    junk = tmp_path / ".easychange" / "manual-junk.bin"
    junk.write_bytes(b"x" * 1024)
    external_junk = window.service.processes.workspace_state_root / "manual-junk.bin"
    external_junk.write_bytes(b"external-junk")
    side_temp = tmp_path / ".main.py.easychange-999.tmp"
    side_temp.write_text("junk", encoding="utf-8")

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args, **kwargs: errors.append(args))

    assert window.cleanup_easychange_button.isVisible()
    window.cleanup_easychange_data()
    app.processEvents()

    assert errors == []
    assert source.read_text(encoding="utf-8") == "print('project stays')\n"
    assert git_keep.exists()
    assert not junk.exists()
    assert not external_junk.exists()
    assert not side_temp.exists()
    assert (tmp_path / ".easychange" / "session.json").exists()
    assert window.cleanup_easychange_button.isVisible()

    window.toggle_machine()
    app.processEvents()
    assert not window.cleanup_easychange_button.isVisible()
    window.close()
    app.processEvents()


def test_evidence_byte_budget_prunes_oldest_without_touching_project_or_referenced_blobs(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYCHANGE_MAX_EVIDENCE_BYTES", str(1024 * 1024))
    monkeypatch.setenv("EASYCHANGE_MAX_STATE_BYTES", str(4 * 1024 * 1024))

    source = tmp_path / "Program.cs"
    source.write_text("class Program {}", encoding="utf-8")

    easy = tmp_path / ".easychange"
    evidence_root = easy / "evidence"
    old_dir = evidence_root / "old"
    new_dir = evidence_root / "new"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    (old_dir / "blob.bin").write_bytes(b"a" * (700 * 1024))
    (new_dir / "blob.bin").write_bytes(b"b" * (700 * 1024))
    now = time.time()
    os.utime(old_dir, (now - 100, now - 100))
    os.utime(new_dir, (now - 10, now - 10))

    referenced_digest = "c" * 64
    blobs = easy / "blobs"
    blobs.mkdir(parents=True)
    referenced = blobs / f"{referenced_digest}.zlib"
    referenced.write_bytes(b"required-undo-snapshot")

    service = MaintenanceService(tmp_path)
    report = service.automatic_cleanup(
        referenced_blobs={referenced_digest},
        active_process_ids=set(),
        now=now,
    )

    assert not old_dir.exists()
    assert new_dir.exists()
    assert referenced.exists()
    assert source.read_text(encoding="utf-8") == "class Program {}"
    assert report["disk_budget"]["evidence_projected_after_bytes"] <= 1024 * 1024
    assert report["disk_budget"]["over_budget"] is False


def test_external_runtime_byte_budget_prunes_terminal_lru_but_preserves_running(tmp_path, monkeypatch):
    from easychange.core.process_service import ProcessService

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    monkeypatch.setenv("EASYCHANGE_MAX_RUNTIME_BYTES", str(1024 * 1024))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ProcessService(workspace)

    # RUNNING state is preserved even when it alone exceeds the quota.
    active_job = service.jobs_root / "JACTIVE.json"
    active_job.write_text(json.dumps({"state": "RUNNING"}), encoding="utf-8")
    active_log = service.root / "JACTIVE.out"
    active_log.write_bytes(b"r" * (1100 * 1024))

    terminal_job = service.jobs_root / "JOLD.json"
    terminal_job.write_text(json.dumps({"state": "SUCCEEDED"}), encoding="utf-8")
    terminal_log = service.root / "JOLD.out"
    terminal_log.write_bytes(b"x" * (700 * 1024))

    now = time.time()
    for path in (active_job, active_log):
        os.utime(path, (now, now))
    for path in (terminal_job, terminal_log):
        os.utime(path, (now - 60, now - 60))

    report = service.maintenance(keep_finished=8)

    assert active_job.exists()
    assert active_log.exists()
    assert not terminal_job.exists()
    assert not terminal_log.exists()
    assert report["disk_reclaimed_bytes"] >= 700 * 1024
    assert report["disk_over_budget"] is True  # active state is intentionally fail-safe preserved


def test_external_runtime_purge_is_scoped_to_easychange_workspace(tmp_path, monkeypatch):
    from easychange.core.process_service import ProcessService

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    important = workspace / "important.txt"
    important.write_text("keep", encoding="utf-8")

    service = ProcessService(workspace)
    junk = service.workspace_state_root / "ef-scripts" / "junk.sql"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_text("SELECT 1;", encoding="utf-8")
    external_root = service.workspace_state_root

    report = service.purge_durable_state()

    assert report["ok"] is True
    assert not external_root.exists()
    assert important.read_text(encoding="utf-8") == "keep"



def test_automatic_cleanup_preserves_active_durable_result_receipts(tmp_path):
    from easychange.core.result_store import DurableResultStore

    results_root = tmp_path / ".easychange" / "results"
    store = DurableResultStore(
        results_root,
        ttl_seconds=60,
        active_ttl_seconds=24 * 60 * 60,
    )
    active = "QMAINTACTIVE1"
    store.mark_running(active, "a" * 64, ":build-job", mutation=True)
    active_path = results_root / f"{active}.json.zlib"
    old = time.time() - 120
    os.utime(active_path, (old, old))

    terminal = "QMAINTTERM1"
    store.mark_running(terminal, "b" * 64, ":state", mutation=False)
    store.complete(
        terminal,
        "b" * 64,
        ":state",
        {
            "ok": True,
            "command": "state",
            "data": {},
            "error": None,
            "code": None,
            "command_id": "C1",
            "duration_ms": 1,
            "sequence": terminal,
        },
    )
    terminal_path = results_root / f"{terminal}.json.zlib"
    os.utime(terminal_path, (old, old))

    report = MaintenanceService(tmp_path).automatic_cleanup(now=time.time())

    assert active_path.exists()
    assert terminal_path.exists()
    assert store.get(terminal)["state"] == "DONE"
    assert report["categories"].get("active_results_stale", 0) == 0


def test_disk_budget_never_uses_active_result_as_pressure_candidate(tmp_path, monkeypatch):
    from easychange.core.result_store import DurableResultStore

    monkeypatch.setenv("EASYCHANGE_MAX_STATE_BYTES", str(1024 * 1024))
    root = tmp_path / ".easychange" / "results"
    store = DurableResultStore(root)
    sequence = "QPRESSUREACTIVE1"
    store.mark_running(sequence, "c" * 64, ":long-mutation", mutation=True)
    active_path = root / f"{sequence}.json.zlib"

    # Make disposable evidence large enough to force the pressure path.
    evidence = tmp_path / ".easychange" / "evidence" / "old"
    evidence.mkdir(parents=True)
    (evidence / "payload.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    _old(evidence, seconds=60)
    _old(evidence / "payload.bin", seconds=60)

    report = MaintenanceService(tmp_path).automatic_cleanup(
        now=time.time(),
        active_process_ids=set(),
        referenced_blobs=set(),
    )

    assert active_path.exists()
    assert not evidence.exists()
    assert "results_pressure" not in report["categories"]



def test_maintenance_bounded_receipt_decoder_matches_canonical_store(tmp_path, monkeypatch):
    import easychange.core.result_store as result_module

    root=tmp_path/".easychange"/"results"
    store=result_module.DurableResultStore(root)
    sequence="QCANONICAL1"
    store.mark_running(sequence,"d"*64,":mutation",mutation=True)
    path=store._path(sequence)
    maintenance=MaintenanceService(tmp_path)
    record=maintenance._result_record(path)
    assert record["state"]=="RUNNING"
    assert record["sequence"]==sequence

    before=path.read_bytes()
    monkeypatch.setattr(result_module,"MAX_COMPRESSED_RECEIPT_BYTES",8)
    monkeypatch.setattr(
        result_module.DurableResultStore, "_decode",
        lambda _data: (_ for _ in ()).throw(
            AssertionError("oversized compressed receipt was decoded")))
    # Invalid/oversized receipts must not be treated as disposable records.
    assert maintenance._result_record(path)=={}
    maintenance.automatic_cleanup(now=time.time()+10*24*3600)
    assert path.read_bytes()==before


def test_maintenance_never_erases_durable_receipts_after_ttl_or_under_pressure(tmp_path):
    from easychange.core.result_store import DurableResultStore

    root = tmp_path / ".easychange" / "results"
    store = DurableResultStore(root,ttl_seconds=60,active_ttl_seconds=3600)
    ids = ("QNEVEREXECUTE1","QNEVERMUTATE1","QNEVERRUNNING1")
    store.mark_running(ids[0],"a"*64,":state",mutation=False)
    store.complete(ids[0],"a"*64,":state",
                   {"ok":True,"command":"state","data":{"text":"completed"},
                    "sequence":ids[0]})
    store.mark_running(ids[1],"b"*64,":write",mutation=True)
    store.complete(ids[1],"b"*64,":write",
                   {"ok":True,"command":"write","data":{"verified":True},
                    "sequence":ids[1]},
                   mutation_receipt={"state":"COMMITTED","lifecycle_state":"APPLIED"})
    store.mark_running(ids[2],"c"*64,":slow",mutation=True)
    broken=root/"QCORRUPT1.json.zlib"
    broken.write_bytes(b"corrupt receipt - never erase")
    old=time.time()-10*24*3600
    for path in root.glob("Q*.json.zlib"):
        os.utime(path,(old,old))

    service=MaintenanceService(tmp_path)
    service.max_state_bytes=1  # Force pressure, even after other candidates vanish.
    report=service.automatic_cleanup(now=time.time())
    for seq in ids:
        assert store.get(seq) is not None
        assert root.joinpath(seq+".json.zlib").exists()
    assert broken.read_bytes()==b"corrupt receipt - never erase"
    assert "results" not in report["categories"]
    assert "results_pressure" not in report["categories"]
    assert "active_results_stale" not in report["categories"]
    assert report["disk_budget"]["over_budget"] is True


def test_maintenance_uses_per_result_retention_without_changing_global_default(tmp_path):
    from easychange.core.result_store import DurableResultStore

    results_root = tmp_path / ".easychange" / "results"
    store = DurableResultStore(
        results_root,
        ttl_seconds=60,
        active_ttl_seconds=3600,
    )
    sequence = "QRETENTIONMETA1"
    record = store.mark_running(
        sequence,
        "a" * 64,
        ":state",
        mutation=False,
    )
    assert record["retention_ttl_seconds"] == 60
    assert record["active_retention_ttl_seconds"] == 3600
