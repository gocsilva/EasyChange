"""A connected local client does not imply a trustworthy MCP tool catalog."""
import unittest
from unittest.mock import Mock, patch

from mcp_adapter.module_service import Handler
from module_bridge import MCPBridge


class HubCatalogContractTests(unittest.TestCase):
    def response(self, members, catalog):
        handler = object.__new__(Handler)
        handler.path = "/hub"
        handler.send = Mock()
        bridge = Mock(spec=MCPBridge)
        bridge.connected = True
        bridge.peers.return_value = members
        bridge.catalog.return_value = catalog
        with patch("module_bridge.MCPBridge", return_value=bridge):
            handler.do_GET()
        bridge.peers.assert_called_once_with()
        bridge.catalog.assert_called_once_with()
        return handler.send.call_args.args

    def test_valid_catalog_and_peer_list_preserve_connected_response(self):
        self.assertEqual(self.response(
            {"ok":True,"self":"easychange","peers":["mcp","esp"]},
            {"ok":True,"tools":[{"name":"example"}]}),
            (200,{"connected":True,"identity":"easychange",
                  "peers":["mcp","esp"],"tools":1}))

    def test_malformed_catalog_cannot_claim_live_mcp_integration(self):
        members={"ok":True,"self":"easychange","peers":["mcp"]}
        for catalog in (
            None, [], {}, {"tools":[]}, {"ok":False,"tools":[]},
            {"ok":True,"tools":"serialized"},
            {"ok":True,"tools":{"fake":"tool"}},
        ):
            with self.subTest(catalog=catalog):
                self.assertEqual(self.response(members,catalog),
                    (503,{"connected":False,
                          "error":"MCP_MODULE_CATALOG_INVALID"}))

    def test_invalid_peers_cannot_claim_live_mcp_integration(self):
        catalog={"ok":True,"tools":[]}
        for peers in (None, "mcp", {"peer":"mcp"}, [None], [1]):
            with self.subTest(peers=peers):
                self.assertEqual(self.response(
                    {"ok":True,"self":"easychange","peers":peers},catalog),
                    (503,{"connected":False,
                          "error":"MCP_MODULE_CATALOG_INVALID"}))


if __name__ == "__main__":
    unittest.main()
