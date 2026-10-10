"""Malformed inherited MCP credentials cannot trigger network I/O."""
import unittest
from unittest.mock import patch

import module_bridge


class BridgeCredentialPresenceTests(unittest.TestCase):
    def test_malformed_explicit_bearer_is_not_connected(self):
        for candidate in (b"A"*48,False,123,"é"*48,"A"*257,
                          chr(0xD800)*48):
            with self.subTest(kind=type(candidate).__name__):
                bridge=module_bridge.MCPBridge(token=candidate)
                self.assertFalse(bridge.connected)
                with patch.object(module_bridge._local_opener,"open") as network:
                    with self.assertRaisesRegex(
                            RuntimeError,"MCP_NOT_CONNECTED_STANDALONE"):
                        bridge.peers()
                    network.assert_not_called()

    def test_invalid_inherited_environment_token_fails_without_http(self):
        for inherited in ("é"*48,"A"*257):
            with self.subTest(length=len(inherited)), \
                 patch.object(module_bridge,"_runtime_token",""), \
                 patch.object(module_bridge,"_runtime_detached",False), \
                 patch.dict(module_bridge.os.environ,
                            {"MCP_MODULE_BUS_TOKEN":inherited}):
                bridge=module_bridge.MCPBridge()
                self.assertFalse(bridge.connected)
                with patch.object(module_bridge._local_opener,"open") as network:
                    with self.assertRaisesRegex(
                            RuntimeError,"MCP_NOT_CONNECTED_STANDALONE"):
                        bridge.peers()
                    network.assert_not_called()

    def test_corrupted_runtime_token_is_not_accepted_as_implicit_bearer(self):
        for active in (42,chr(0xD800)*48,"A"*257):
            with self.subTest(kind=type(active).__name__), \
                 patch.object(module_bridge,"_runtime_token",active), \
                 patch.object(module_bridge,"_runtime_detached",False), \
                 patch.dict(module_bridge.os.environ,
                            {"MCP_MODULE_BUS_TOKEN":"A"*48}):
                self.assertFalse(module_bridge.MCPBridge().connected)

    def test_valid_ascii_credentials_keep_standalone_bridge_compatible(self):
        token="A"*48
        with patch.object(module_bridge,"_runtime_token",""), \
             patch.object(module_bridge,"_runtime_detached",False), \
             patch.dict(module_bridge.os.environ,
                        {"MCP_MODULE_BUS_TOKEN":token}):
            self.assertTrue(module_bridge.MCPBridge().connected)
            self.assertTrue(module_bridge.MCPBridge(token=token).connected)


if __name__ == "__main__":
    unittest.main()
