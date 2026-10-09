"""MCP credentials must never be forwarded outside the configured loopback hub."""
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.error import HTTPError
import module_bridge

class TestHubRedirectBoundary(unittest.TestCase):
    def test_no_external_proxy_config(self):
        from urllib.request import ProxyHandler
        handlers=[h for h in module_bridge._local_opener.handlers
                  if isinstance(h,ProxyHandler)]
        # urllib may omit a ProxyHandler with no proxy mappings entirely.
        self.assertFalse(any(h.proxies for h in handlers))

    def test_esp_presence_cannot_follow_redirect_or_inherited_proxy(self):
        from urllib.request import ProxyHandler
        import mcp_adapter.module_service as service

        handlers=service.urlopen.__self__.handlers
        self.assertFalse(any(isinstance(h,ProxyHandler) and h.proxies
                             for h in handlers))
        restricted=next(
            h for h in handlers if isinstance(h,service._NoLocalPeerRedirect))
        self.assertIsNone(restricted.redirect_request(
            None,None,None,None,None,None))

    def test_esp_presence_requires_matching_service_identity(self):
        import io
        import json
        from unittest.mock import patch
        from mcp_adapter.module_service import esp_online

        cases=(
            ({"ok":True,"id":"esp"},True),
            ({"ok":True},False),
            ({"ok":True,"id":"android"},False),
            ({"ok":False,"id":"esp"},False),
            ({"ok":1,"id":"esp"},False),
            ([{"ok":True,"id":"esp"}],False),
        )
        for status,expected in cases:
            with self.subTest(status=status), patch(
                    "mcp_adapter.module_service.urlopen",
                    return_value=io.BytesIO(json.dumps(status).encode("utf-8"))):
                self.assertIs(esp_online(),expected)
        with patch("mcp_adapter.module_service.urlopen",
                   side_effect=TimeoutError("SERVICE_UNAVAILABLE")):
            self.assertFalse(esp_online())

    def test_esp_health_read_is_bounded_even_for_untrusted_local_service(self):
        import io
        from unittest.mock import patch
        from mcp_adapter.module_service import esp_online

        limits=[]
        class ControlledResponse(io.BytesIO):
            def read(self,limit=-1):
                limits.append(limit)
                return super().read(limit)

        large=ControlledResponse(
            b'{"ok":true,"id":"esp","padding":"' + b"x"*3000 + b'"}')
        with patch("mcp_adapter.module_service.urlopen",return_value=large):
            self.assertFalse(esp_online())
        self.assertEqual(limits,[2049])

        with patch("mcp_adapter.module_service.urlopen",
                   return_value=io.BytesIO(b'{"ok":')):
            self.assertFalse(esp_online())

    def test_tool_rpc_busy_retry_only_when_hub_confirms_not_executed(self):
        import io
        import json
        from unittest.mock import Mock,patch
        from urllib.error import HTTPError

        def rejection(status,error,accepted=None):
            payload={"error":error,"accepted":accepted,"retryable":True}
            return HTTPError("http://127.0.0.1:8786/v1/tools/call",
                             status,"rejected",{},
                             io.BytesIO(json.dumps(payload).encode("utf-8")))

        client=module_bridge.MCPBridge(token="local-test-token")
        completed={"ok":True,"result":{"value":42}}
        client._request=Mock(side_effect=[
            rejection(429,"MODULE_TOOL_RPC_BUSY_RETRY_LATER",False),completed])
        with patch("module_bridge.time.sleep") as sleep:
            self.assertEqual(client.call("mcp_core_status",ping=1),completed)
        self.assertEqual(client._request.call_count,2)
        self.assertEqual(client._request.call_args_list[0],
                         client._request.call_args_list[1])
        sleep.assert_called_once_with(0.025)

        # 429 from the tool itself may happen after executing side effects.
        for status,error,accepted in (
                (429,"TOOL_RATE_LIMIT",False),
                (429,"MODULE_TOOL_RPC_BUSY_RETRY_LATER",True),
                (502,"INTERNAL_TOOL_ERROR",None)):
            with self.subTest(status=status,error=error,accepted=accepted):
                client._request=Mock(side_effect=rejection(status,error,accepted))
                with patch("module_bridge.time.sleep") as sleep:
                    with self.assertRaises(HTTPError):
                        client.call("write_once")
                self.assertEqual(client._request.call_count,1)
                sleep.assert_not_called()

    def test_tool_rpc_retry_has_finite_budget_and_never_replays_timeout(self):
        import io
        import json
        from unittest.mock import Mock,patch
        from urllib.error import HTTPError

        def busy():
            data={"error":"MODULE_TOOL_RPC_BUSY_RETRY_LATER",
                  "accepted":False,"retryable":True}
            return HTTPError("http://127.0.0.1:8786/v1/tools/call",
                             429,"busy",{},
                             io.BytesIO(json.dumps(data).encode("utf-8")))

        client=module_bridge.MCPBridge(token="local-test-token")
        client._request=Mock(side_effect=[busy() for _ in range(7)])
        with patch("module_bridge.time.sleep") as sleep:
            with self.assertRaises(HTTPError):
                client.call("slow_tool")
        self.assertEqual(client._request.call_count,7)
        self.assertEqual(sleep.call_count,6)

        client._request=Mock(side_effect=TimeoutError("RESPONSE_UNKNOWN"))
        with patch("module_bridge.time.sleep") as sleep:
            with self.assertRaises(TimeoutError):
                client.call("possibly_mutating_tool")
        self.assertEqual(client._request.call_count,1)
        sleep.assert_not_called()

    def test_hub_response_is_bounded_before_json_decode(self):
        import io
        from unittest.mock import MagicMock,patch

        client=module_bridge.MCPBridge(token="local-test-token")
        healthy=io.BytesIO(b'{"ok":true,"self":"easychange"}')
        with patch.object(module_bridge._local_opener,"open",return_value=healthy) as opener:
            self.assertEqual(client.peers()["self"],"easychange")
        opener.assert_called_once()

        maximum=8_388_608+131_072
        oversized=MagicMock()
        oversized.read.return_value=b"x"*(maximum+1)
        oversized.__enter__.return_value=oversized
        with patch.object(module_bridge._local_opener,"open",return_value=oversized):
            with patch.object(module_bridge.json,"loads",
                              side_effect=AssertionError("unbounded parse")):
                with self.assertRaisesRegex(ValueError,"MCP_HUB_RESPONSE_TOO_LARGE"):
                    client.peers()
        oversized.read.assert_called_once_with(maximum+1)

    def test_hub_response_rejects_non_object_json(self):
        import io
        from unittest.mock import patch
        client=module_bridge.MCPBridge(token="local-test-token")
        with patch.object(module_bridge._local_opener,"open",
                          return_value=io.BytesIO(b'[1,2,3]')):
            with self.assertRaisesRegex(ValueError,"INVALID_MCP_HUB_RESPONSE"):
                client.peers()

    def test_post_redirect_handler_denies_forwarding(self):
        from urllib.request import HTTPRedirectHandler
        handlers=[h for h in module_bridge._local_opener.handlers
                  if isinstance(h,HTTPRedirectHandler)]
        self.assertEqual(len(handlers),1)
        self.assertIsInstance(handlers[0],module_bridge._NoRedirect)

    def test_hub_redirect_cannot_relay_bearer_to_another_server(self):
        arrivals=[]
        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):
                arrivals.append(self.headers.get("Authorization"))
                data=b'{"ok":true,"self":"wrong-server"}'
                self.send_response(200)
                self.send_header("Content-Length",str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self,*args):pass
        destination=ThreadingHTTPServer(("127.0.0.1",0),Destination)
        url="http://127.0.0.1:"+str(destination.server_port)+"/stolen"
        class Redirector(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location",url)
                self.send_header("Content-Length","0")
                self.end_headers()
            def log_message(self,*args):pass
        redirector=ThreadingHTTPServer(("127.0.0.1",0),Redirector)
        threads=[]
        for server in (destination,redirector):
            t=threading.Thread(target=server.serve_forever,daemon=True)
            t.start()
            threads.append(t)
        old_url=module_bridge.BUS
        module_bridge.BUS="http://127.0.0.1:"+str(redirector.server_port)
        try:
            client=module_bridge.MCPBridge(token="TEST_BEARER_DO_NOT_FORWARD")
            with self.assertRaises(HTTPError) as rejected:
                client.peers()
            self.assertEqual(rejected.exception.code,302)
            self.assertEqual(arrivals,[])
        finally:
            module_bridge.BUS=old_url
            for server in (destination,redirector):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=3)
