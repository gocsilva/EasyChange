"""Read-only hub metadata must not inherit a tool's long execution timeout."""
from unittest.mock import patch

from module_bridge import MCPBridge


def test_metadata_calls_use_short_bounded_timeouts():
    client=MCPBridge(token="local-catalog-test-token")
    with patch.object(client,"_request",return_value={"ok":True}) as request:
        assert client.catalog()=={"ok":True}
        request.assert_called_once_with("/v1/tools",timeout=8)
    with patch.object(client,"_request",return_value={"ok":True}) as request:
        assert client.peers()=={"ok":True}
        request.assert_called_once_with("/v1/peers",timeout=3)


def test_tool_execution_preserves_long_timeout_even_after_metadata_probe():
    client=MCPBridge(token="local-catalog-test-token")
    with patch.object(client,"_request",return_value={"ok":True}) as request:
        assert client.call("nonmutating_example",ping=True)=={"ok":True}
        request.assert_called_once_with(
            "/v1/tools/call",
            {"tool":"nonmutating_example","arguments":{"ping":True}},
            timeout=120,
        )
