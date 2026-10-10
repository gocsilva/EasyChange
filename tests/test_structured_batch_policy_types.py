"""A textual false must not silently enable structured batch continuation."""
from __future__ import annotations

import pytest

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


@pytest.mark.parametrize("policy", ("false", "true", 0, 1, None, [], {}))
def test_non_boolean_continue_policy_rejected_before_dispatch(tmp_path, monkeypatch, policy):
    service=CommandService(Workspace.open(tmp_path))
    try:
        calls=[]
        def forbidden(*args, **kwargs):
            calls.append((args,kwargs))
            raise AssertionError("INVALID_FLAG_DISPATCHED")
        monkeypatch.setattr(service,"execute_tokens",forbidden)
        batch={"op":"batch","continue_on_error":policy,
               "operations":[{"type":"read","path":"missing.txt"}]}
        result=service._execute_structured(batch,persist=False)
        assert result.ok is False
        assert result.code=="INVALID_STRUCTURED_PAYLOAD"
        assert result.error=="CONTINUE_ON_ERROR_BOOLEAN_REQUIRED"
        assert calls==[]
    finally:
        service.close()


def test_default_continue_policy_remains_valid(tmp_path):
    service=CommandService(Workspace.open(tmp_path))
    try:
        batch={"op":"batch","operations":[
            {"type":"read","path":"missing.txt"}]}
        result=service._execute_structured(batch,persist=False)
        assert result.error!="CONTINUE_ON_ERROR_BOOLEAN_REQUIRED"
    finally:
        service.close()
