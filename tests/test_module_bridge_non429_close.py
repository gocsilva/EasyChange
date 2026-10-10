"""Rejected hub HTTP operations must release sockets without retrying work."""
from __future__ import annotations

import io
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from module_bridge import MCPBridge


@pytest.mark.parametrize("operation", ("call", "send", "ack"))
@pytest.mark.parametrize("code", (401, 403, 500))
def test_rejected_hub_operation_closes_response_without_retry(operation, code):
    body = io.BytesIO(b'{"error":"REJECTED"}')
    error = HTTPError("http://127.0.0.1:8786/v1/send", code,
                      "failure", {}, body)
    bridge = MCPBridge(token="local-test-token")
    bridge._request = Mock(side_effect=error)
    with pytest.raises(HTTPError):
        if operation == "call":
            bridge.call("write_once")
        elif operation == "send":
            bridge.send("esp", "audit.event", {"ok": True})
        else:
            bridge.ack([123])
    assert bridge._request.call_count == 1
    assert body.closed is True
