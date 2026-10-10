"""Standalone service must emit valid JSON even for malformed module diagnostics."""
from __future__ import annotations

import io
import json
import math
from unittest.mock import Mock

import pytest

from mcp_adapter.module_service import Handler, BoundedLocalHTTPServer
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


def test_local_server_overload_never_spawns_more_workers():
    import threading
    from http.server import ThreadingHTTPServer

    server=object.__new__(BoundedLocalHTTPServer)
    server._client_slots=threading.BoundedSemaphore(1)
    server.shutdown_request=Mock()
    with patch.object(ThreadingHTTPServer,"process_request") as dispatch:
        server.process_request("first",("127.0.0.1",10001))
        dispatch.assert_called_once_with("first",("127.0.0.1",10001))
        server.process_request("second",("127.0.0.1",10002))
        dispatch.assert_called_once()
    server.shutdown_request.assert_called_once_with("second")

    with patch.object(ThreadingHTTPServer,"process_request_thread") as process:
        server.process_request_thread("first",("127.0.0.1",10001))
    process.assert_called_once_with("first",("127.0.0.1",10001))
    with patch.object(ThreadingHTTPServer,"process_request") as dispatch:
        server.process_request("third",("127.0.0.1",10003))
        dispatch.assert_called_once_with("third",("127.0.0.1",10003))


def test_local_server_failed_worker_start_returns_slot_and_closes_socket():
    import threading
    from http.server import ThreadingHTTPServer

    server=object.__new__(BoundedLocalHTTPServer)
    server._client_slots=threading.BoundedSemaphore(1)
    server.shutdown_request=Mock()
    with patch.object(ThreadingHTTPServer,"process_request",
                      side_effect=RuntimeError("WORKER_START_FAILED")):
        with pytest.raises(RuntimeError,match="WORKER_START_FAILED"):
            server.process_request("socket",("127.0.0.1",10004))
    server.shutdown_request.assert_called_once_with("socket")
    assert server._client_slots.acquire(blocking=False)
    server._client_slots.release()


def test_local_server_failed_handler_releases_capacity():
    import threading
    from http.server import ThreadingHTTPServer

    server=object.__new__(BoundedLocalHTTPServer)
    server._client_slots=threading.BoundedSemaphore(1)
    assert server._client_slots.acquire(blocking=False)
    with patch.object(ThreadingHTTPServer,"process_request_thread",
                      side_effect=OSError("BROKEN_PIPE")):
        with pytest.raises(OSError,match="BROKEN_PIPE"):
            server.process_request_thread("socket",("127.0.0.1",10005))
    assert server._client_slots.acquire(blocking=False)
    server._client_slots.release()


def test_unicode_encoder_budget_never_encodes_huge_intermediate_fragment():
    codepoint_windows=[]

    class ObservedText(str):
        def encode(self,encoding="utf-8",errors="strict"):
            codepoint_windows.append(len(self))
            assert len(self)<=4096
            return super().encode(encoding,errors)

    class SyntheticEncoder:
        def __init__(self,**_kwargs):
            pass
        def iterencode(self,_value):
            yield ObservedText("🙂"*5000)

    with patch.object(module_service,"MAX_SERVICE_RESPONSE_BYTES",16400), \
         patch.object(module_service.json,"JSONEncoder",SyntheticEncoder):
        with pytest.raises(ValueError,match="MODULE_SERVICE_RESPONSE_TOO_LARGE"):
            module_service._bounded_service_json({"synthetic":True})
    assert codepoint_windows==[4096,904]


def test_large_unicode_response_keeps_exact_json_wire_format():
    payload={"text":"á🙂"*2200}
    encoded=module_service._bounded_service_json(payload)
    assert json.loads(encoded)==payload


def test_service_response_rejects_ambiguous_mapping_keys_before_encoding():
    import pytest
    samples=(
        {1:"number"},
        {"ok":True,"nested":{True:"bool"}},
        {"ok":True,"nested":{1:"integer","1":"string"}},
        {None:"null-key"},
    )
    for payload in samples:
        with patch.object(module_service.json.JSONEncoder,"iterencode",
                          side_effect=AssertionError("ENCODER_CALLED")) as encoder:
            with pytest.raises(ValueError,match="INVALID_SERVICE_RESPONSE"):
                module_service._bounded_service_json(payload)
            encoder.assert_not_called()
    expected={"ok":True,"nested":{"1":"string","false":False}}
    assert json.loads(module_service._bounded_service_json(expected))==expected


def test_service_json_preflight_bounded_on_wide_and_cyclic_containers():
    import pytest
    cyclic={"ok":True}
    cyclic["recursive"]=[cyclic]
    with patch.object(module_service.json.JSONEncoder,"iterencode",
                      side_effect=AssertionError("ENCODER_CALLED")) as encoder:
        with pytest.raises(ValueError,match="INVALID_SERVICE_RESPONSE"):
            module_service._bounded_service_json(cyclic)
        encoder.assert_not_called()
        with patch.object(module_service,"MAX_SERVICE_RESPONSE_BYTES",8):
            with pytest.raises(ValueError,match="MODULE_SERVICE_RESPONSE_TOO_LARGE"):
                module_service._bounded_service_json({"items":list(range(9))})
        encoder.assert_not_called()


def test_oversized_json_mapping_keys_rejected_before_encoder_allocation():
    examples=(
        {"x"*257:1},
        {"ok":True,"nested":{"long_key_"*33:"value"}},
    )
    with patch.object(module_service,"MAX_SERVICE_RESPONSE_BYTES",256), \
         patch.object(module_service.json.JSONEncoder,"iterencode",
                      side_effect=AssertionError("ENCODER_ALLOCATED")) as encoder:
        for example in examples:
            with pytest.raises(ValueError,match="MODULE_SERVICE_RESPONSE_TOO_LARGE"):
                module_service._bounded_service_json(example)
        encoder.assert_not_called()

    accepted={"ok":True,"nested":{"short":"value"}}
    assert json.loads(module_service._bounded_service_json(accepted))==accepted
