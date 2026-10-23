from __future__ import annotations

import subprocess
import json
from pathlib import Path
from types import SimpleNamespace

from easychange.core.command_metadata import command_schema
from easychange.core.command_service import CommandService
from easychange.core.operation_registry import TEXT_MUTATION_COMMANDS
from easychange.core.workspace import Workspace
from easychange import self_update


def test_self_update_is_documented_as_a_sequenced_mutation():
    schema = command_schema("self-update")
    assert schema["mutating"] is True
    assert schema["syntax"] == "self-update"
    assert "self-update" in TEXT_MUTATION_COMMANDS


def test_self_update_status_is_documented_as_read_only():
    schema = command_schema("self-update-status")
    assert schema["mutating"] is False
    assert schema["syntax"] == "self-update-status"


def test_switch_project_schema_is_generic_and_includes_recovery_example():
    schema = command_schema("switch-project")
    assert schema["mutating"] is True
    assert schema["arguments"][0]["name"] == "workspace_path"
    assert any("C:\\sources\\EasyPay\\bs2.core2.pix.cambio" in item for item in schema["examples"])


def test_project_commands_validate_paths_and_emit_sequenced_requests(tmp_path):
    target = tmp_path / "remote project"
    target.mkdir()
    service = CommandService(Workspace.open(tmp_path), defer_initial_maintenance=True)
    try:
        switch = service.execute(f':ec QSWITCH123 switch-project "{target}"')
        assert switch.ok is True
        assert switch.sequence == "QSWITCH123"
        assert switch.data["switch_project"] == str(target.resolve())

        update = service.execute(":ec QUPDATE123 self-update")
        assert update.ok is True
        assert update.sequence == "QUPDATE123"
        assert update.data["update_request"] == "github_main"
        assert update.data["restart_mode"] == "machine_hid"

        missing = service.execute(':switch-project "C:\\path\\that\\does\\not\\exist"')
        assert missing.ok is False
    finally:
        service.close()


def test_machine_gui_switches_workspace_in_same_window(tmp_path, monkeypatch):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from easychange.ui import main_window as ui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "remember_project", lambda _path: None)
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    window = ui.MainWindow(Workspace.open(source), machine_mode=True, hid_mode=True)
    try:
        window._switch_workspace_path(target, show_remote=False)
        assert window.service.workspace.root_path == target.resolve()
        assert window.service.machine is True
        assert window.service.output == "compact"
        assert window._machine is True
        assert window.windowTitle().endswith("target")
    finally:
        window.close()
        app.processEvents()


def test_schedule_self_update_starts_worker_for_same_workspace(tmp_path, monkeypatch):
    repo = tmp_path / "EasyChange"
    repo.mkdir()
    (repo / "Atualizar_EasyChange.bat").write_text("@echo off\n", encoding="utf-8")
    workspace = tmp_path / "project with spaces"
    workspace.mkdir()
    log_path = tmp_path / "logs" / "update.log"
    receipt_path = tmp_path / "logs" / "update-receipt.json"
    monkeypatch.setattr(self_update, "update_log_path", lambda: log_path)
    monkeypatch.setattr(self_update, "update_receipt_path", lambda: receipt_path)
    captured = {}

    def popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=42)

    result = self_update.schedule_self_update(
        workspace,
        repo_root=repo,
        executable="pythonw.exe",
        parent_pid=12,
        platform_name="nt",
        popen_factory=popen,
    )

    assert result["ok"] is True
    assert result["workspace"] == str(workspace.resolve())
    assert result["restart_mode"] == "machine_hid"
    assert result["restart_grace_seconds"] == 45
    assert result["worker_pid"] == 42
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["state"] == "SCHEDULED"
    assert receipt["worker_pid"] == 42
    assert receipt["workspace"] == str(workspace.resolve())
    assert captured["argv"][-6:] == [
        "--workspace", str(workspace.resolve()), "--parent-pid", "12",
        "--executable", "pythonw.exe",
    ]
    assert captured["kwargs"]["cwd"] == str(repo.resolve())
    assert log_path.exists()


def test_schedule_self_update_records_worker_start_failure(tmp_path, monkeypatch):
    repo = tmp_path / "EasyChange"
    repo.mkdir()
    (repo / "Atualizar_EasyChange.bat").write_text("@echo off\n", encoding="utf-8")
    workspace = tmp_path / "project"
    workspace.mkdir()
    log_path = tmp_path / "logs" / "update.log"
    receipt_path = tmp_path / "logs" / "update-receipt.json"
    monkeypatch.setattr(self_update, "update_log_path", lambda: log_path)
    monkeypatch.setattr(self_update, "update_receipt_path", lambda: receipt_path)

    def fail_to_start(*_args, **_kwargs):
        raise OSError("worker launch failed")

    result = self_update.schedule_self_update(
        workspace,
        repo_root=repo,
        executable="pythonw.exe",
        parent_pid=12,
        platform_name="nt",
        popen_factory=fail_to_start,
    )

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert result["error"] == "UPDATE_WORKER_START_FAILED"
    assert receipt["state"] == "SCHEDULE_FAILED"
    assert receipt["relaunch_state"] == "NOT_STARTED"
    assert receipt["schedule_error"].startswith("OSError:")


def test_update_worker_runs_github_updater_then_reopens_same_workspace(tmp_path, monkeypatch):
    repo = tmp_path / "EasyChange"
    repo.mkdir()
    updater = repo / "Atualizar_EasyChange.bat"
    updater.write_text("@echo off\n", encoding="utf-8")
    workspace = tmp_path / "target project"
    workspace.mkdir()
    log_path = tmp_path / "logs" / "update.log"
    receipt_path = tmp_path / "logs" / "update-receipt.json"
    monkeypatch.setattr(self_update, "update_log_path", lambda: log_path)
    monkeypatch.setattr(self_update, "update_receipt_path", lambda: receipt_path)
    monkeypatch.setattr(
        self_update,
        "_read_git_revisions",
        lambda _root: {
            "head_sha": "c" * 40,
            "origin_main_sha": "c" * 40,
            "matches_origin_main": True,
        },
    )
    monkeypatch.setenv("COMSPEC", "cmd-test.exe")
    calls = {}

    def run(argv, **kwargs):
        calls["update_argv"] = argv
        calls["update_kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    def popen(argv, **kwargs):
        calls["restart_argv"] = argv
        calls["restart_kwargs"] = kwargs
        return SimpleNamespace(pid=99)

    monkeypatch.setattr(subprocess, "Popen", popen)
    result = self_update.run_update_worker(
        repo_root=repo,
        workspace=workspace,
        parent_pid=123,
        executable="pythonw.exe",
        run=run,
        wait_parent=lambda _pid: True,
        sleep=lambda _seconds: None,
    )

    assert result == 0
    assert calls["update_argv"] == ["cmd-test.exe", "/d", "/c", str(updater.resolve())]
    assert calls["update_kwargs"]["cwd"] == str(repo.resolve())
    assert calls["update_kwargs"]["env"]["EASYCHANGE_NONINTERACTIVE"] == "1"
    assert int(calls["update_kwargs"]["creationflags"]) & 0x08000000
    assert calls["restart_argv"] == [
        "pythonw.exe", "-m", "easychange.gui", str(workspace.resolve()), "--machine", "--hid"
    ]
    assert calls["restart_kwargs"]["cwd"] == str(repo.resolve())
    assert "updater_return_code=0" in log_path.read_text(encoding="utf-8")


def test_update_worker_persists_verifiable_git_receipt(tmp_path, monkeypatch):
    repo = tmp_path / "EasyChange"
    repo.mkdir()
    (repo / "Atualizar_EasyChange.bat").write_text("@echo off\n", encoding="utf-8")
    workspace = tmp_path / "project"
    workspace.mkdir()
    log_path = tmp_path / "logs" / "update.log"
    receipt_path = tmp_path / "logs" / "update-receipt.json"
    monkeypatch.setattr(self_update, "update_log_path", lambda: log_path)
    monkeypatch.setattr(self_update, "update_receipt_path", lambda: receipt_path)
    sha = "a" * 40
    monkeypatch.setattr(
        self_update,
        "_read_git_revisions",
        lambda _root: {
            "head_sha": sha,
            "origin_main_sha": sha,
            "matches_origin_main": True,
        },
    )
    monkeypatch.setenv("COMSPEC", "cmd-test.exe")
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: SimpleNamespace(pid=99),
    )

    result = self_update.run_update_worker(
        repo_root=repo,
        workspace=workspace,
        parent_pid=123,
        executable="pythonw.exe",
        run=lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
        wait_parent=lambda _pid: True,
        sleep=lambda _seconds: None,
    )

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    status = self_update.read_update_status(repo_root=repo)
    assert result == 0
    assert receipt["state"] == "UPDATED"
    assert receipt["installed_git_sha"] == sha
    assert receipt["origin_main_sha"] == sha
    assert receipt["relaunch_state"] == "PROCESS_STARTED"
    assert status["current_matches_receipt"] is True
    assert status["current_matches_origin_main"] is True
    assert status["updater_return_code"] == 0


def test_self_update_status_text_command_reads_receipt_without_mutating(tmp_path, monkeypatch):
    receipt_path = tmp_path / "update-receipt.json"
    monkeypatch.setattr(self_update, "update_receipt_path", lambda: receipt_path)
    monkeypatch.setattr(
        self_update,
        "_read_git_revisions",
        lambda _root: {"head_sha": "b" * 40, "origin_main_sha": "b" * 40,
                       "matches_origin_main": True},
    )
    receipt_path.write_text(json.dumps({
        "schema": self_update.UPDATE_RECEIPT_SCHEMA,
        "state": "UPDATED",
        "updater_return_code": 0,
        "installed_git_sha": "b" * 40,
        "origin_main_sha": "b" * 40,
        "relaunch_state": "PROCESS_STARTED",
        "workspace": str(tmp_path),
        "finished_at_utc": "2026-10-20T00:00:00+00:00",
    }), encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path), defer_initial_maintenance=True)
    try:
        result = service.execute(":ec QSTATUS123 self-update-status")
        assert result.ok is True
        assert result.data["state"] == "UPDATED"
        assert result.data["installed_git_sha"] == "b" * 40
        assert result.data["current_matches_receipt"] is True
        assert result.sequence == "QSTATUS123"
    finally:
        service.close()


def test_self_update_rejects_non_windows(tmp_path):
    result = self_update.schedule_self_update(tmp_path, platform_name="posix")
    assert result == {"ok": False, "error": "SELF_UPDATE_REQUIRES_WINDOWS"}


def test_unsaved_editor_guard_compares_disk_and_can_discard_easychange(tmp_path, monkeypatch):
    from easychange.ui import main_window as ui
    MainWindow = ui.MainWindow

    project = tmp_path / "project"
    project.mkdir()
    same_file = project / "same.cs"
    same_file.write_text("same text", encoding="utf-8")
    changed_file = project / "changed.cs"
    changed_file.write_text("disk text", encoding="utf-8")
    install = tmp_path / "EasyChange"
    install_file = install / "easychange.py"
    install_file.parent.mkdir(parents=True)
    install_file.write_text("disk version", encoding="utf-8")
    monkeypatch.setattr(ui, "installation_root", lambda: install)

    def window(*, modified, text, active_path=None):
        editor = SimpleNamespace(
            document=lambda: SimpleNamespace(isModified=lambda: modified),
            toPlainText=lambda: text,
        )
        workspace = SimpleNamespace(
            resolve=lambda path, must_exist=False: Path(path).resolve(strict=must_exist)
        )
        return SimpleNamespace(
            editor=editor,
            _active_path=active_path,
            service=SimpleNamespace(workspace=workspace),
        )

    assert MainWindow._has_unsaved_editor_changes(
        window(modified=True, text="")
    ) is False
    assert MainWindow._has_unsaved_editor_changes(
        window(modified=True, text="pending text")
    ) is True
    assert MainWindow._has_unsaved_editor_changes(
        window(modified=True, text="same text", active_path=same_file)
    ) is False
    assert MainWindow._has_unsaved_editor_changes(
        window(modified=True, text="memory text", active_path=changed_file)
    ) is True
    assert MainWindow._has_unsaved_editor_changes(
        window(modified=True, text="memory version", active_path=install_file),
        discard_easychange=True,
    ) is False
    assert MainWindow._has_unsaved_editor_changes(
        window(modified=True, text="memory version", active_path=install_file)
    ) is True
    assert MainWindow._has_unsaved_editor_changes(
        window(modified=False, text="saved text")
    ) is False


def test_machine_command_queues_update_and_keeps_receipt_visible(monkeypatch, tmp_path):
    import json
    from easychange.core.result import Result
    from easychange.remote.optical_protocol import encode_result_bundle
    from easychange.ui import main_window as ui

    result = Result(
        True,
        "self-update",
        data={"update_request": "github_main"},
        sequence="QUPDATE999",
    )
    calls = {"scheduled": [], "timers": []}
    monkeypatch.setattr(
        ui,
        "schedule_self_update",
        lambda workspace, parent_pid: calls["scheduled"].append((workspace, parent_pid))
        or {
            "ok": True,
            "scheduled": True,
            "workspace": str(workspace),
            "restart_mode": "machine_hid",
            "restart_grace_seconds": self_update.RESTART_GRACE_SECONDS,
        },
    )
    monkeypatch.setattr(
        ui.QTimer,
        "singleShot",
        lambda delay, callback: calls["timers"].append((delay, callback)),
    )
    output = SimpleNamespace(setPlainText=lambda _value: None)
    editor = SimpleNamespace(
        document=lambda: SimpleNamespace(isModified=lambda: True),
        toPlainText=lambda: "",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    window = SimpleNamespace(
        editor=editor,
        _active_path=None,
        service=SimpleNamespace(
            execute=lambda _text: result,
            workspace=SimpleNamespace(root_path=workspace),
            machine=True,
            output="compact",
        ),
        _machine=True,
        _update_restart_scheduled=False,
        _refresh_header=lambda: None,
        _sync_optical_transport_controls=lambda: None,
        focus_command=lambda: {"command_focus_confirmed": True},
        output=output,
        _update_machine_qr=lambda _result: None,
        close=lambda: None,
    )
    window._has_unsaved_editor_changes = lambda **kwargs: ui.MainWindow._has_unsaved_editor_changes(
        window, **kwargs
    )

    ui.MainWindow._run_command(window, ":ec QUPDATE999 self-update")

    assert calls["scheduled"][0][0] == workspace
    assert window._update_restart_scheduled is True
    assert calls["timers"] == [(45_000, window.close)]
    assert result.data == {
        "scheduled": True,
        "restart_mode": "machine_hid",
    }
    packets, _header = encode_result_bundle(result.to_wire_dict())
    assert len(packets) == 1
    assert len(json.dumps(result.to_wire_dict(), separators=(",", ":"))) < 180


def test_update_wait_parent_openprocess_access_denied_does_not_allow_update(monkeypatch):
    """Access-denied is NOT proof old EasyChange GUI exited."""
    from types import SimpleNamespace
    kernel=SimpleNamespace(
        OpenProcess=lambda *_:0,
        GetLastError=lambda:5,  # ERROR_ACCESS_DENIED
    )
    monkeypatch.setattr(self_update.os,"name","nt")
    monkeypatch.setattr(self_update.ctypes,"windll",SimpleNamespace(kernel32=kernel),
                        raising=False)
    assert self_update._wait_for_parent_exit(4567) is False


def test_update_wait_parent_pid_not_found_permits_safe_handoff(monkeypatch):
    """ERROR_INVALID_PARAMETER means no such parent PID exists."""
    from types import SimpleNamespace
    kernel=SimpleNamespace(
        OpenProcess=lambda *_:0,
        GetLastError=lambda:87,  # ERROR_INVALID_PARAMETER / no process
    )
    monkeypatch.setattr(self_update.os,"name","nt")
    monkeypatch.setattr(self_update.ctypes,"windll",SimpleNamespace(kernel32=kernel),
                        raising=False)
    assert self_update._wait_for_parent_exit(4567) is True
