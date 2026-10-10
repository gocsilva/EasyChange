"""Reject oversized RPC/mailbox strings before JSON copies or network I/O."""
from __future__ import annotations

import io
import unittest
from unittest.mock import patch

import module_bridge


class ModuleBridgePreflightBudgetTests(unittest.TestCase):
    def setUp(self):
        self.client=module_bridge.MCPBridge(token="preflight-test-token")

    def test_oversized_nested_strings_fail_before_serializing(self):
        scenarios=(
            ("/v1/send",{"payload":"Z"*18000}),
            ("/v1/inbox/ack",{"ids":["Z"*18000]}),
            ("/v1/tools/call",{"tool":"test","arguments":{
                "nested":{"text":"Z"*1_048_577}}}),
        )
        for route,payload in scenarios:
            with self.subTest(route=route), \
                 patch.object(module_bridge.json,"dumps",
                              side_effect=AssertionError("SERIALIZED_HUGE_INPUT")) as encoded, \
                 patch.object(module_bridge._local_opener,"open") as network:
                with self.assertRaisesRegex(ValueError,"MCP_BUS_REQUEST_TOO_LARGE"):
                    self.client._request(route,payload)
                encoded.assert_not_called()
                network.assert_not_called()

    def test_cyclic_structures_fail_without_network_or_unbounded_walk(self):
        cyclic={}
        cyclic["self"]=cyclic
        with patch.object(module_bridge._local_opener,"open") as network:
            with self.assertRaises(ValueError):
                self.client._request("/v1/send",{"payload":cyclic})
        network.assert_not_called()

    def test_small_valid_request_preserves_authenticated_wire_contract(self):
        with patch.object(module_bridge._local_opener,"open",
                          return_value=io.BytesIO(b'{"accepted":true,"id":41}')) as network:
            result=self.client._request("/v1/send",{
                "target":"esp","topic":"status.ping","payload":{"n":1}})
        self.assertEqual(result,{"accepted":True,"id":41})
        request=network.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"),
                         "Bearer preflight-test-token")
        self.assertIn(b'"status.ping"',request.data)


if __name__=="__main__":
    unittest.main()
