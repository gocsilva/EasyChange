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


@pytest.mark.parametrize("route", ("/v1/attach", "/v1/detach"))
@pytest.mark.parametrize("length", (
    "9"*60000, "1e309", "１２", "1 2", "-3", "00000000000", "NaN",
))
def test_control_rejects_malformed_content_length_without_reading(route, length):
    handler=object.__new__(Handler)
    handler.path=route
    handler.headers={"Content-Type":"application/json",
                     "Content-Length":length}
    handler.rfile=Mock()
    handler.send=Mock()
    handler.do_POST()
    handler.send.assert_called_once_with(
        400,{"error":"INVALID_CONTENT_LENGTH"})
    handler.rfile.read.assert_not_called()


def test_control_preserves_size_cap_for_valid_numeric_headers():
    handler=object.__new__(Handler)
    handler.path="/v1/attach"
    handler.headers={"Content-Type":"application/json", "Content-Length":"513"}
    handler.rfile=Mock()
    handler.send=Mock()
    handler.do_POST()
    handler.send.assert_called_once_with(
        413,{"error":"PAYLOAD_TOO_LARGE"})
    handler.rfile.read.assert_not_called()
