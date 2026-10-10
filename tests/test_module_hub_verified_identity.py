"""Standalone /hub must never claim an unrelated or unverified MCP identity."""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from mcp_adapter.module_service import Handler


class HubIdentityTests(unittest.TestCase):
    def setUp(self):
        self.handler=object.__new__(Handler)
        self.handler.path="/hub"
        self.reply=Mock()
        self.handler.send=self.reply

    def test_mismatched_or_unverified_hub_never_claims_connected(self):
        bridge=Mock()
        bridge.connected=True
        with patch("module_bridge.MCPBridge",return_value=bridge):
            for peers in ({"ok":True,"self":"another-module"},
                          {"ok":"true","self":"easychange"},
                          {"ok":False,"self":"easychange"},
                          {"ok":True},[],None):
                with self.subTest(peers=peers):
                    self.reply.reset_mock()
                    bridge.catalog.reset_mock()
                    bridge.peers.return_value=peers
                    self.handler.do_GET()
                    self.reply.assert_called_once_with(503,{
                        "connected":False,"error":"MCP_MODULE_IDENTITY_MISMATCH"})
                    bridge.catalog.assert_not_called()

    def test_valid_hub_and_standalone_keep_their_original_shape(self):
        bridge=Mock()
        with patch("module_bridge.MCPBridge",return_value=bridge):
            bridge.connected=False
            self.handler.do_GET()
            self.reply.assert_called_once_with(200,{
                "connected":False,"standalone":True})
            bridge.peers.assert_not_called()
            self.reply.reset_mock()
            bridge.connected=True
            bridge.peers.return_value={
                "ok":True,"self":"easychange","peers":["android"]}
            bridge.catalog.return_value={"tools":[{"name":"echo"}]}
            self.handler.do_GET()
            self.reply.assert_called_once_with(200,{
                "connected":True,"identity":"easychange",
                "peers":["android"],"tools":1})


if __name__=="__main__":
    unittest.main()
