"""EasyChange owner-owned autonomous update and status command contract."""
from pathlib import Path
from types import SimpleNamespace

from easychange.core.command_metadata import command_schema
from easychange.core.operation_registry import TEXT_MUTATION_COMMANDS
from easychange.core.workspace import Workspace
from easychange.core.command_service import CommandService
from easychange.core.result import Result
from easychange import self_update
from easychange.ui import main_window as ui


def test_autonomous_update_is_registered_mutation():
    meta=command_schema("autonomous-update")
    assert meta["mutating"] is True
    assert "autonomous-update" in TEXT_MUTATION_COMMANDS
    assert command_schema("autonomous-update-status")["mutating"] is False


def test_autonomous_update_reuses_durable_handoff_without_starting_gui(tmp_path):
    service=CommandService(Workspace.open(tmp_path), defer_initial_maintenance=True)
    try:
        outcome=service.execute(":ec QAUTOMAIN123 autonomous-update")
        assert outcome.ok and outcome.sequence=="QAUTOMAIN123"
        assert outcome.data["update_request"]=="github_main"
        assert outcome.data["restart_mode"]=="machine_hid"
    finally:
        service.close()


def test_autonomous_status_is_nonmutating_and_fresh(monkeypatch,tmp_path):
    monkeypatch.setattr(self_update,"read_update_status",
                        lambda:{"state":"UPDATED","installed_git_sha":"f"*40})
    service=CommandService(Workspace.open(tmp_path),defer_initial_maintenance=True)
    try:
        outcome=service.execute(":autonomous-update-status")
        assert outcome.ok is True
        assert outcome.data["state"]=="UPDATED"
        assert outcome.data["installed_git_sha"]=="f"*40
    finally:
        service.close()


def test_autonomous_update_refuses_to_restart_with_unsaved_editor_changes(monkeypatch, tmp_path):
    from easychange.ui.main_window import MainWindow

    active_file = tmp_path / "source.py"
    active_file.write_text("saved version", encoding="utf-8")
    workspace = SimpleNamespace(
        root_path=tmp_path,
        resolve=lambda path, must_exist=False: Path(path).resolve(strict=must_exist),
    )
    command_result = Result(
        True,
        "autonomous-update",
        data={"update_request": "github_main"},
        sequence="QAUTOUNSAVED1",
    )
    scheduled = []
    timers = []
    monkeypatch.setattr(
        ui,
        "schedule_self_update",
        lambda *args, **kwargs: scheduled.append((args, kwargs)) or {
            "ok": True,
            "restart_mode": "machine_hid",
        },
    )
    monkeypatch.setattr(
        ui.QTimer,
        "singleShot",
        lambda *args: timers.append(args),
    )
    output = SimpleNamespace(setPlainText=lambda _text: None)
    window = SimpleNamespace(
        editor=SimpleNamespace(
            document=lambda: SimpleNamespace(isModified=lambda: True),
            toPlainText=lambda: "unsaved version",
        ),
        _active_path=active_file,
        service=SimpleNamespace(
            execute=lambda _text: command_result,
            workspace=workspace,
            machine=False,
            output="compact",
        ),
        _machine=False,
        _update_restart_scheduled=False,
        _refresh_header=lambda: None,
        _sync_optical_transport_controls=lambda: None,
        focus_command=lambda: {"command_focus_confirmed": True},
        output=output,
        _update_machine_qr=lambda _result: None,
        close=lambda: None,
    )
    window._has_unsaved_editor_changes = lambda **kwargs: MainWindow._has_unsaved_editor_changes(
        window, **kwargs
    )

    MainWindow._run_command(window, ":ec QAUTOUNSAVED1 autonomous-update")

    assert scheduled == []
    assert timers == []
    assert window._update_restart_scheduled is False
