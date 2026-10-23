"""Macro parameter substitution must address placeholders exactly once."""
from easychange.core.command_service import CommandService
from easychange.core.result import Result
from easychange.core.workspace import Workspace


def _capture_macro_run(tmp_path, monkeypatch, command, values):
    service = CommandService(Workspace.open(tmp_path))
    captured = []
    service.macros["probe"] = [command]
    monkeypatch.setattr(
        service, "execute",
        lambda value: captured.append(value) or Result(True, "probe"),
    )
    try:
        result = service._macro(["run", "probe", *values])
        assert result["macro"] == "probe"
        return captured
    finally:
        service.close()


def test_macro_substitutes_tenth_argument_without_matching_first_placeholder(
    tmp_path, monkeypatch,
):
    values = ["FIRST", *[f"ARG{number}" for number in range(2, 10)], "TENTH"]
    assert _capture_macro_run(tmp_path, monkeypatch, ":state $10", values) == [
        ":state TENTH"
    ]


def test_macro_does_not_rescan_placeholder_text_inside_argument(tmp_path, monkeypatch):
    assert _capture_macro_run(
        tmp_path, monkeypatch, ":state $1 $2", ["$2", "SECOND"],
    ) == [":state $2 SECOND"]
