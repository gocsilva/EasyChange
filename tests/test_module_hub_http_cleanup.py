"""Hub diagnostics must close failed MCP HTTP response sockets."""
from __future__ import annotations

import io
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from mcp_adapter.module_service import Handler


class HubDiagnosticSocketCleanupTests(unittest.TestCase):
    def _invoke(self, code, *, during_catalog=False, send_raises=False):
        backing=io.BytesIO(b"private error body")
        error=HTTPError("http://127.0.0.1:8786/v1/peers",
                        code,"rejected",{},backing)
        handler=object.__new__(Handler)
        handler.path="/hub"
        reply=Mock(side_effect=OSError("REPLY_FAILED") if send_raises else None)
        handler.send=reply
        bridge=Mock()
        bridge.connected=True
        bridge.peers.side_effect=(None if during_catalog else error)
        bridge.peers.return_value={"ok":True,"self":"easychange","peers":[]}
        bridge.catalog.side_effect=(error if during_catalog else None)
        with patch("module_bridge.MCPBridge",return_value=bridge):
            if send_raises:
                with self.assertRaisesRegex(OSError,"REPLY_FAILED"):
                    handler.do_GET()
            else:
                handler.do_GET()
                reply.assert_called_once_with(
                    503,{"connected":False,"error":"HTTPError"})
        self.assertTrue(backing.closed)
        if during_catalog:
            bridge.peers.assert_called_once()
            bridge.catalog.assert_called_once()
        else:
            bridge.catalog.assert_not_called()

    def test_peer_http_rejection_closes_underlying_socket(self):
        for code in (401,403,429,503):
            with self.subTest(code=code):
                self._invoke(code)

    def test_catalog_http_rejection_closes_underlying_socket(self):
        self._invoke(503,during_catalog=True)

    def test_failed_local_error_reply_still_closes_hub_response(self):
        self._invoke(401,send_raises=True)


if __name__=="__main__":
    unittest.main()
