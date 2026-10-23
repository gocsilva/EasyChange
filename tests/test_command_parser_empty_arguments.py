"""CLI command parser must preserve explicit empty-quoted arguments."""
import pytest
from easychange.core.command_parser import CommandParser


@pytest.mark.parametrize("quoted",['""',"''"])
def test_parser_preserves_empty_replacement_argument(quoted):
    parser=CommandParser()
    assert parser.parse(f':replace "source.cs" "old-value" {quoted}') == [
        "replace", "source.cs", "old-value", "",
    ]


def test_parser_preserves_empty_first_argument_with_following_tokens():
    parser=CommandParser()
    assert parser.parse(':command "" second') == ["command", "", "second"]
    assert parser._tokenize('command "" second') == ["command", "", "second"]
