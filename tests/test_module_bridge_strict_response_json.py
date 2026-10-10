"""Ambiguous hub fields must not authorize retrying mutating operations."""
from __future__ import annotations

import io
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import module_bridge


class StrictHubReplyTests(unittest.TestCase):
    def test_duplicate_json_fields_cannot_override_message_outcome(self):
        for raw in (
            b'{"accepted":true,"accepted":false}',
            b'{"result":{"state":"DONE","state":"RUNNING"}}',
            b'{"retryable":true,"retryable":false}',
        ):
            with self.subTest(raw=raw), self.assertRaisesRegex(
                    ValueError,"MCP_HUB_DUPLICATE_JSON_KEY"):
                module_bridge._decode_hub_response(raw)

    def test_nonfinite_hub_reply_is_rejected(self):
        for value in (b'NaN',b'Infinity',b'1e999'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module_bridge._decode_hub_response(
                    b'{"accepted":false,"retryable":'+value+b'}')

    def test_duplicate_busy_fields_cannot_trigger_tool_replay(self):
        ambiguous=(
            b'{"error":"MODULE_TOOL_RPC_BUSY_RETRY_LATER",'
            b'"accepted":true,"accepted":false,"retryable":true}')
        client=module_bridge.MCPBridge(token="test-hub-bearer")
        error=HTTPError("http://127.0.0.1:8786/v1/tools/call",
                        429,"busy",{},io.BytesIO(ambiguous))
        client._request=Mock(side_effect=error)
        with patch.object(module_bridge.time,"sleep") as delay:
            with self.assertRaises(HTTPError):
                client.call("write_file",path="target.txt")
        self.assertEqual(client._request.call_count,1)
        delay.assert_not_called()

    def test_duplicate_busy_fields_cannot_trigger_send_replay(self):
        ambiguous=(
            b'{"error":"MODULE_INBOX_FULL_RETRY_LATER",'
            b'"accepted":true,"accepted":false,"retryable":true}')
        client=module_bridge.MCPBridge(token="test-hub-bearer")
        error=HTTPError("http://127.0.0.1:8786/v1/send",
                        429,"busy",{},io.BytesIO(ambiguous))
        client._request=Mock(side_effect=error)
        with patch.object(module_bridge.time,"sleep") as delay:
            with self.assertRaises(HTTPError):
                client.send("mcp","sample",{"once":True})
        self.assertEqual(client._request.call_count,1)
        delay.assert_not_called()

    def test_authenticated_regular_reply_rejects_duplicate_metadata(self):
        client=module_bridge.MCPBridge(token="test-hub-bearer")
        response=io.BytesIO(b'{"ok":true,"ok":false}')
        with patch.object(module_bridge._local_opener,"open",
                          return_value=response):
            with self.assertRaisesRegex(
                    ValueError,"MCP_HUB_DUPLICATE_JSON_KEY"):
                client.peers()


if __name__=="__main__":
    unittest.main()
