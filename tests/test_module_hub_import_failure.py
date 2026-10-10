"""Standalone HTTP routes must reply even if optional hub bridge fails to import."""
from __future__ import annotations

import builtins
import io
from unittest.mock import Mock,patch

from mcp_adapter.module_service import Handler

def _import_without_hub():
    original=builtins.__import__
    def fail_bridge(name,globals=None,locals=None,fromlist=(),level=0):
        if name=="module_bridge":
            raise ModuleNotFoundError("optional hub client not installed")
        return original(name,globals,locals,fromlist,level)
    return fail_bridge


def _handler(route):
    handler=object.__new__(Handler)
    handler.path=route
    handler.send=Mock()
    return handler


def test_hub_get_has_recoverable_503_if_optional_bridge_import_fails():
    handler=_handler("/hub")
    with patch("builtins.__import__",side_effect=_import_without_hub()):
        handler.do_GET()
    handler.send.assert_called_once_with(
        503,{"connected":False,"error":"HUB_CLIENT_UNAVAILABLE"})


def test_attach_post_does_not_drop_connection_when_bridge_import_fails():
    handler=_handler("/v1/attach")
    body=b'{"token":"example-test-credential"}'
    handler.headers={"Content-Type":"application/json",
                     "Content-Length":str(len(body))}
    handler.rfile=io.BytesIO(body)
    with patch("builtins.__import__",side_effect=_import_without_hub()):
        handler.do_POST()
    handler.send.assert_called_once_with(
        503,{"error":"HUB_UNAVAILABLE"})


def _invalid_hub_configuration():
    original=builtins.__import__
    def fail_bridge(name,globals=None,locals=None,fromlist=(),level=0):
        if name=="module_bridge":
            raise ValueError("MCP_BUS_MUST_USE_LOOPBACK")
        return original(name,globals,locals,fromlist,level)
    return fail_bridge


def test_invalid_optional_hub_url_returns_recoverable_status():
    handler=_handler("/hub")
    with patch("builtins.__import__",side_effect=_invalid_hub_configuration()):
        handler.do_GET()
    handler.send.assert_called_once_with(
        503,{"connected":False,"error":"HUB_CLIENT_UNAVAILABLE"})


def test_invalid_optional_hub_url_does_not_misclassify_valid_attach_token():
    handler=_handler("/v1/attach")
    body=b'{"token":"example-test-credential"}'
    handler.headers={"Content-Type":"application/json",
                     "Content-Length":str(len(body))}
    handler.rfile=io.BytesIO(body)
    with patch("builtins.__import__",side_effect=_invalid_hub_configuration()):
        handler.do_POST()
    handler.send.assert_called_once_with(
        503,{"error":"HUB_UNAVAILABLE"})
