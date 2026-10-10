"""Reject ambiguous standalone HTTP request framing before body or module mutation."""
from __future__ import annotations

from email.message import Message
from unittest.mock import Mock, patch

import pytest
from mcp_adapter.module_service import Handler


def request(path, *, extra):
    headers=Message()
    headers.add_header("Content-Type","application/json")
    headers.add_header("Content-Length","50")
    if extra=="duplicate-type":
        headers.add_header("Content-Type","application/json")
    elif extra=="transfer-encoding":
        headers.add_header("Transfer-Encoding","chunked")
    handler=object.__new__(Handler)
    handler.path=path
    handler.headers=headers
    handler.rfile=Mock()
    handler.send=Mock()
    return handler


@pytest.mark.parametrize("extra,code",(
    ("duplicate-type","INVALID_CONTENT_TYPE"),
    ("transfer-encoding","UNSUPPORTED_TRANSFER_ENCODING"),
))
@pytest.mark.parametrize("path",("/v1/attach","/v1/detach",))
def test_invalid_framing_cannot_parse_attach_or_execute_tool(path,extra,code):
    handler=request(path,extra=extra)
    with patch("module_bridge.attach_hub",
               side_effect=AssertionError("ATTACH_EXECUTED")) as attach:
        handler.do_POST()
    handler.send.assert_called_once_with(400,{"error":code})
    handler.rfile.read.assert_not_called()
    attach.assert_not_called()


def test_normal_standalone_attach_content_type_is_still_accepted():
    import io
    body=b'{"token":"test-only-token"}'
    headers=Message()
    headers.add_header("Content-Type","application/json; charset=utf-8")
    headers.add_header("Content-Length",str(len(body)))
    handler=object.__new__(Handler)
    handler.path="/v1/attach"
    handler.headers=headers
    handler.rfile=io.BytesIO(body)
    handler.send=Mock()
    with patch("module_bridge.attach_hub",
               return_value={"connected":True}) as attach:
        handler.do_POST()
    attach.assert_called_once()
    handler.send.assert_called_once_with(200,{"connected":True})
