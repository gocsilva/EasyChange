"""EasyChange owner-owned autonomous update and status command contract."""
from easychange.core.command_metadata import command_schema
from easychange.core.operation_registry import TEXT_MUTATION_COMMANDS
from easychange.core.workspace import Workspace
from easychange.core.command_service import CommandService
from easychange import self_update


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
