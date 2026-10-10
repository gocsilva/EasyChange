"""Standalone service must emit valid JSON even for malformed module diagnostics."""
from __future__ import annotations

import io
import json
import math
from unittest.mock import Mock

import pytest

from mcp_adapter.module_service import Handler
import mcp_adapter.module_service as module_service
from unittest.mock import patch


def render(status, payload):
    handler=object.__new__(Handler)
    handler.send_response=Mock()
    handler.send_header=Mock()
    handler.end_headers=Mock()
    handler.wfile=io.BytesIO()
    handler.send(status,payload)
    raw=handler.wfile.getvalue()
    return handler.send_response.call_args.args[0],json.loads(raw),raw


@pytest.mark.parametrize("value",(math.nan,math.inf,-math.inf,object()))
def test_nonfinite_or_unserializable_diagnostics_return_valid_error_json(value):
    status,body,raw=render(200,{"ready":value})
    assert status==502
    assert body=={"error":"INVALID_SERVICE_RESPONSE"}
    assert b"NaN" not in raw and b"Infinity" not in raw


def test_cyclic_diagnostics_rejected_without_half_written_http_response():
    looping=[]
    looping.append(looping)
    status,body,raw=render(200,{"nested":looping})
    assert status==502
    assert body=={"error":"INVALID_SERVICE_RESPONSE"}
    assert raw==b'{"error":"INVALID_SERVICE_RESPONSE"}'


def test_normal_standalone_health_keeps_http_shape_and_unicode():
    status,body,raw=render(200,{"ok":True,"id":"easychange","label":"ação"})
    assert status==200
    assert body=={"ok":True,"id":"easychange","label":"ação"}
    assert "ação".encode("utf-8") in raw


def test_bounded_service_reply_rejects_oversize_before_adding_encoded_payload():
    with patch.object(module_service,"MAX_SERVICE_RESPONSE_BYTES",64):
        status,body,raw=render(200,{"payload":"X"*512})
    assert status==502
    assert body=={"error":"MODULE_SERVICE_RESPONSE_TOO_LARGE"}
    assert raw==b'{"error":"MODULE_SERVICE_RESPONSE_TOO_LARGE"}'


def test_utf8_budget_uses_bytes_not_code_point_count():
    with patch.object(module_service,"MAX_SERVICE_RESPONSE_BYTES",60):
        status,body,raw=render(200,{"label":"ação"*30})
        assert status==502
        assert body=={"error":"MODULE_SERVICE_RESPONSE_TOO_LARGE"}
        status,body,raw=render(200,{"label":"ok"})
        assert status==200
        assert body=={"label":"ok"}


def test_socket_deadline_is_set_before_base_http_setup():
    instance=object.__new__(Handler)
    instance.request=Mock()
    with patch("mcp_adapter.module_service.BaseHTTPRequestHandler.setup") as base_setup:
        instance.setup()
    instance.request.settimeout.assert_called_once_with(12.0)
    base_setup.assert_called_once_with()
