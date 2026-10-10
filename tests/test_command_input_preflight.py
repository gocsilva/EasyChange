"""Oversized HID/EC1 text must fail before expensive parsing or mutation."""
from __future__ import annotations

from unittest.mock import patch

import pytest

import easychange.core.command_service as command_module
from easychange.core.command_service import CommandService


@pytest.mark.parametrize("prefix", [
    ":ec QPRECHECK1 :j1 ", ":batch ", ":macro define sample ",
    ":u1 ", "write sample.txt ",
])
def test_too_large_command_is_rejected_without_parsing(prefix,monkeypatch):
    # A tiny injected cap verifies the real entrypoint without allocating
    # tens of MiB of test data.
    monkeypatch.setattr(command_module,"MAX_COMMAND_INPUT_CHARS",32)
    service=object.__new__(CommandService)
    with patch.object(CommandService,"_execute_sequenced",
                      side_effect=AssertionError("sequence accepted")), \
         patch.object(CommandService,"_execute_inbound_upload_control",
                      side_effect=AssertionError("upload accepted")), \
         patch.object(CommandService,"execute_tokens",
                      side_effect=AssertionError("parser accepted")):
        result=service.execute(prefix+"z"*80)
    assert result.ok is False
    assert result.code=="COMMAND_INPUT_TOO_LARGE"


@pytest.mark.parametrize("invalid",[None,1,False,b"payload",[],{}])
def test_nontext_command_is_rejected_without_coercion(invalid):
    service=object.__new__(CommandService)
    result=service.execute(invalid)
    assert result.ok is False
    assert result.code=="INVALID_COMMAND"


def test_empty_text_still_uses_original_validation():
    service=object.__new__(CommandService)
    result=service.execute(" \n\t ")
    assert result.ok is False
    assert result.code=="INVALID_COMMAND"
