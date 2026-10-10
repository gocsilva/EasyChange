"""Informational hub HTTP failures must not leak the local response socket."""
from __future__ import annotations

import io
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from module_bridge import MCPBridge


@pytest.mark.parametrize("path", (
    "/v1/tools", "/v1/peers", "/v1/inbox/peek", "/v1/inbox",
))
@pytest.mark.parametrize("status", (401, 429, 500))
def test_rejected_read_only_hub_requests_close_error_responses(path, status):
    response=io.BytesIO(b'{"error":"REJECTED"}')
    error=HTTPError("http://127.0.0.1:8786"+path,status,
                    "rejected",{},response)
    client=MCPBridge(token="test-explicit-token")
    with patch("module_bridge._local_opener.open",side_effect=error) as opener:
        with pytest.raises(HTTPError) as caught:
            client._request(path)
    assert caught.value.code==status
    opener.assert_called_once()
    assert response.closed is True


@pytest.mark.parametrize("path", (
    "/v1/tools/call", "/v1/send", "/v1/inbox/ack",
))
def test_mutation_rejections_remain_readable_for_exactly_once_policy(path):
    response=io.BytesIO(b'{"accepted":false,"retryable":true}')
    error=HTTPError("http://127.0.0.1:8786"+path,429,
                    "admission rejected",{},response)
    client=MCPBridge(token="test-explicit-token")
    with patch("module_bridge._local_opener.open",side_effect=error):
        with pytest.raises(HTTPError):
            client._request(path)
    # The caller, not the low-level transport, must inspect the complete 429
    # admission proof and then close the error before considering any retry.
    assert response.closed is False
    error.close()
    assert response.closed is True
