from __future__ import annotations

import subprocess
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
    monkeypatch.setattr(self_update, "update_log_path", lambda: log_path)
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
    assert captured["argv"][-6:] == [
        "--workspace", str(workspace.resolve()), "--parent-pid", "12",
        "--executable", "pythonw.exe",
    ]
    assert captured["kwargs"]["cwd"] == str(repo.resolve())
    assert log_path.exists()


def test_update_worker_runs_github_updater_then_reopens_same_workspace(tmp_path, monkeypatch):
    repo = tmp_path / "EasyChange"
    repo.mkdir()
    updater = repo / "Atualizar_EasyChange.bat"
    updater.write_text("@echo off\n", encoding="utf-8")
    workspace = tmp_path / "target project"
    workspace.mkdir()
    log_path = tmp_path / "logs" / "update.log"
    monkeypatch.setattr(self_update, "update_log_path", lambda: log_path)
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
    assert calls["restart_argv"] == [
        "pythonw.exe", "-m", "easychange.gui", str(workspace.resolve()), "--machine", "--hid"
    ]
    assert calls["restart_kwargs"]["cwd"] == str(repo.resolve())
    assert "updater_return_code=0" in log_path.read_text(encoding="utf-8")


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
    from easychange.core.result import Result
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
    assert result.data["scheduled"] is True
