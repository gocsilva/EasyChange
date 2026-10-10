"""Standalone service must emit valid JSON even for malformed module diagnostics."""
from __future__ import annotations

import io
import json
import math
from unittest.mock import Mock

import pytest

from mcp_adapter.module_service import Handler


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
