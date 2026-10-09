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
