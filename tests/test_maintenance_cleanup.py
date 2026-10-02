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
    assert not result_file.exists()
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
    assert not side_temp.exists()
    assert (tmp_path / ".easychange" / "session.json").exists()
    assert window.cleanup_easychange_button.isVisible()

    window.toggle_machine()
    app.processEvents()
    assert not window.cleanup_easychange_button.isVisible()
    window.close()
    app.processEvents()
