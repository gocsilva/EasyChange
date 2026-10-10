"""Exercise the real authenticated HTTP path for bounded inbox pages.

Mock only urllib network I/O. A mocked MCPBridge._request would conceal
an allowlist regression and let the consumer silently fail at runtime.
"""
from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

import module_bridge
from module_bridge import MCPBridge


class ReadTracking(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.limits=[]

    def read(self, n=-1):
        self.limits.append(n)
        return super().read(n)


class PagedInboxHTTPTests(unittest.TestCase):
    def setUp(self):
        self.bridge=MCPBridge(token="A"*48)

    def test_small_inbox_pages_use_real_allowlisted_http_endpoint(self):
        page={"ok":True,"requires_ack":True,"recipient":module_bridge.CANONICAL_MODULE_ID,
              "messages":[],"pending":0}
        wire=json.dumps(page).encode("utf-8")
        for limit in (1,7,19,20):
            with self.subTest(limit=limit):
                response=ReadTracking(wire)
                with patch.object(module_bridge._local_opener,"open",
                                  return_value=response) as opener:
                    self.assertEqual(self.bridge.receive(limit=limit),page)
                sent=opener.call_args.args[0]
                suffix="/v1/inbox/peek"+(f"?limit={limit}" if limit!=20 else "")
                self.assertTrue(sent.full_url.endswith(suffix))
                self.assertEqual(sent.get_method(),"GET")
                self.assertEqual(sent.get_header("Authorization"),"Bearer "+"A"*48)
                self.assertEqual(response.limits,[2_097_153])

    def test_paged_read_accepts_large_valid_mailbox_envelope(self):
        page={"ok":True,"requires_ack":True,"recipient":module_bridge.CANONICAL_MODULE_ID,
              "messages":[],"padding":"p"*70000}
        with patch.object(module_bridge._local_opener,"open",
                          return_value=ReadTracking(json.dumps(page).encode())):
            result=self.bridge.receive(limit=7)
        self.assertEqual(result,page)

    def test_no_arbitrary_query_variant_reaches_hub(self):
        bad=(
            "/v1/inbox/peek?limit=0",
            "/v1/inbox/peek?limit=21",
            "/v1/inbox/peek?limit=01",
            "/v1/inbox/peek?limit=1&next=http://elsewhere",
            "/v1/inbox/peek?limit=1%0a",
            "/v1/inbox/peek?limit=true",
            "/v1/inbox/peek?limit=-1",
            "/v1/peers?limit=1",
        )
        with patch.object(module_bridge._local_opener,"open") as opener:
            for path in bad:
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError,"MCP_BUS_PATH_NOT_ALLOWED"):
                        self.bridge._request(path)
        opener.assert_not_called()


if __name__=="__main__":
    unittest.main()
