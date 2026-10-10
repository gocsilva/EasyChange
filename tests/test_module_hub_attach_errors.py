"""Attach authorization failures must not be reported as transient MCP outages."""
import io
import json
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError

from mcp_adapter.module_service import Handler


class HubAttachErrorContractTests(unittest.TestCase):
    def request(self, failure):
        handler=object.__new__(Handler)
        handler.path="/v1/attach"
        wire=json.dumps({"token":"opaque-private-test-token"}).encode("utf-8")
        handler.headers={"Content-Type":"application/json",
                         "Content-Length":str(len(wire))}
        handler.rfile=io.BytesIO(wire)
        handler.send=Mock()
        with patch("module_bridge.attach_hub",side_effect=failure):
            handler.do_POST()
        return handler.send.call_args.args

    def test_rejected_hub_bearer_is_a_permission_error(self):
        for code in (401,403):
            with self.subTest(code=code):
                error=HTTPError("http://127.0.0.1:8786/v1/peers",
                                code,"secret-private-denial",{},io.BytesIO(b""))
                self.assertEqual(self.request(error),
                    (403,{"error":"HUB_CREDENTIAL_REJECTED"}))

    def test_hub_network_and_internal_failures_remain_recoverable(self):
        for error in (
            HTTPError("http://127.0.0.1:8786/v1/peers",
                      503,"private-diagnostic",{},io.BytesIO(b"")),
            OSError("secret-host-not-reachable"),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.request(error),
                    (503,{"error":"HUB_UNAVAILABLE"}))


if __name__=="__main__":
    unittest.main()
