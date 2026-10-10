"""Keep JSON preflight memory independent of wide RPC container breadth."""
from __future__ import annotations

import tracemalloc
import unittest
from unittest.mock import patch

from module_bridge import MCPBridge


class _StopAfterPreflight(Exception):
    pass


class ModuleBridgeLazyPreflightTests(unittest.TestCase):
    def test_wide_rpc_preflight_does_not_copy_the_entire_child_list(self):
        bridge=MCPBridge(token="test-local-token")
        # Allocate before starting the memory trace: only preflight temporary
        # allocations are relevant, not the caller's existing payload.
        request={"tool":"sample","arguments":{"values":[0]*100_000}}
        with patch("module_bridge.json.JSONEncoder.iterencode",
                   side_effect=_StopAfterPreflight):
            tracemalloc.start()
            try:
                with self.assertRaises(_StopAfterPreflight):
                    bridge._request("/v1/tools/call",request)
                _current,peak=tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        # A 100k-entry temporary list used to cost ~800kB here.
        self.assertLess(peak,650_000)

    def test_cyclic_payload_fails_without_reaching_http(self):
        bridge=MCPBridge(token="test-local-token")
        cyclic=[]
        cyclic.append(cyclic)
        with patch("module_bridge._local_opener.open") as post:
            with self.assertRaisesRegex(ValueError,"MCP_BUS_REQUEST_TOO_LARGE"):
                bridge._request(
                    "/v1/tools/call",
                    {"tool":"sample","arguments":{"cycle":cyclic}})
        post.assert_not_called()


if __name__=="__main__":
    unittest.main()
