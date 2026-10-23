"""Batch terminators must not truncate ordinary chained command arguments."""
from easychange.core.command_service import CommandService
from easychange.core.result import Result
from easychange.core.workspace import Workspace


def test_non_batch_chain_preserves_end_marker_text_in_quoted_argument(
    tmp_path, monkeypatch,
):
    service = CommandService(Workspace.open(tmp_path))
    calls = []

    def record_tokens(name, args, *, raw=None):
        calls.append((name, args, raw))
        return Result(True, name)

    monkeypatch.setattr(service, "execute_tokens", record_tokens)
    monkeypatch.setattr(service, "_persist_state", lambda _result: None)
    try:
        reply = service.execute(':search "foo:endbar";:state')
        assert reply.ok is True
        assert calls == [
            ("search", ["foo:endbar"], ':search "foo:endbar"'),
            ("state", [], ":state"),
        ]
    finally:
        service.close()


def test_batch_preserves_end_suffix_when_it_is_not_a_standalone_marker(
    tmp_path, monkeypatch,
):
    service = CommandService(Workspace.open(tmp_path))
    calls = []

    def record_tokens(name, args, *, raw=None):
        calls.append((name, args, raw))
        return Result(True, name)

    monkeypatch.setattr(service, "execute_tokens", record_tokens)
    monkeypatch.setattr(service, "_persist_state", lambda _result: None)
    try:
        reply = service.execute(":batch :search foo:end")
        assert reply.ok is True
        assert calls[1] == ("search", ["foo:end"], ":search foo:end")
        assert calls[-1][0] == "commit"
    finally:
        service.close()
