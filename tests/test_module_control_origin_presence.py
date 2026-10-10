"""A present HTTP Origin header cannot authorize attach/detach, even if blank."""
from __future__ import annotations

from unittest.mock import Mock
import pytest

from mcp_adapter.module_service import Handler


@pytest.mark.parametrize("route", ("/v1/attach", "/v1/detach"))
@pytest.mark.parametrize("origin", ("", "null", "https://untrusted.example"))
def test_control_rejects_all_present_origin_headers_before_body_read(route, origin):
    handler=object.__new__(Handler)
    handler.path=route
    handler.headers={"Origin":origin, "Content-Type":"application/json",
                     "Content-Length":"32"}
    handler.rfile=Mock()
    handler.send=Mock()
    handler.do_POST()
    handler.send.assert_called_once_with(
        403,{"error":"BROWSER_ORIGIN_NOT_ALLOWED"})
    handler.rfile.read.assert_not_called()


@pytest.mark.parametrize("route", ("/v1/attach", "/v1/detach"))
def test_missing_origin_preserves_normal_content_type_validation(route):
    handler=object.__new__(Handler)
    handler.path=route
    handler.headers={"Content-Type":"text/plain","Content-Length":"32"}
    handler.rfile=Mock()
    handler.send=Mock()
    handler.do_POST()
    handler.send.assert_called_once_with(
        415,{"error":"JSON_REQUIRED"})
    handler.rfile.read.assert_not_called()
