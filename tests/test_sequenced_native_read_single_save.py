"""Sequenced native reads persist RUNNING and DONE without an extra snapshot."""
from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def test_sequenced_native_read_uses_only_running_and_done_saves(tmp_path, monkeypatch):
    (tmp_path / "sample.cs").write_text("a\nb\nc\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path), defer_initial_maintenance=True)
    saves = []
    original_persist = service._persist_state

    def record_save(result=None):
        saves.append(getattr(result, "command", None))
        return original_persist(result)

    monkeypatch.setattr(service, "_persist_state", record_save)
    try:
        cmd = ":ec QONEREAD01 :read sample.cs 1 2"
        result = service.execute(cmd)
        assert result.ok
        assert result.command == "read"
        assert result.data["returned_lines"] == 2
        # RUNNING preflight must persist first, final DONE snapshot after
        # the complete durable result. The intermediate save is redundant.
        assert saves == [None, "read"]
        durable = service.result_store.get("QONEREAD01")
        assert durable is not None
        assert durable["state"] == "DONE"
        duplicate = service.execute(cmd)
        assert duplicate.ok
        assert duplicate.data["duplicate"] is True
        assert saves == [None, "read"]
    finally:
        service.close()


def test_sequenced_write_keeps_existing_persistence_and_receipt(tmp_path, monkeypatch):
    (tmp_path / "sample.cs").write_text("a\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path), defer_initial_maintenance=True)
    calls = []
    original = service._persist_state
    def record(result=None):
        calls.append(getattr(result, "command", None))
        return original(result)
    monkeypatch.setattr(service, "_persist_state", record)
    try:
        result = service.execute(":ec QWRITEONCE1 :write sample.cs 'changed'")
        assert result.ok
        assert len(calls) >= 3
        assert service.result_store.get("QWRITEONCE1")["state"] == "DONE"
    finally:
        service.close()
