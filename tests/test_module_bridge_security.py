"""MCP credentials must never be forwarded outside the configured loopback hub."""
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.error import HTTPError
import module_bridge

class TestHubRedirectBoundary(unittest.TestCase):
    def test_duplicate_attach_token_cannot_change_live_bus_credentials(self):
        import io
        from unittest.mock import Mock,patch
        from mcp_adapter.module_service import Handler

        for payload in (
                b'{"token":"safe","token":"other"}',
                b'{"token":"safe","metadata":{"id":"one","id":"two"}}'):
            with self.subTest(payload=payload):
                handler=object.__new__(Handler)
                handler.path="/v1/attach"
                handler.headers={"Content-Type":"application/json",
                                 "Content-Length":str(len(payload))}
                handler.rfile=io.BytesIO(payload)
                handler.send=Mock()
                with patch("module_bridge.attach_hub",
                           side_effect=AssertionError("UNSAFE_ATTACH")) as attach:
                    handler.do_POST()
                attach.assert_not_called()
                handler.send.assert_called_once_with(
                    403,{"error":"ValueError"})

    def test_duplicate_peer_health_cannot_fake_esp_connectivity(self):
        import io
        from unittest.mock import patch
        from mcp_adapter.module_service import esp_online
        raw=b'{"ok":false,"ok":true,"id":"esp"}'
        with patch("mcp_adapter.module_service.urlopen",
                   return_value=io.BytesIO(raw)):
            self.assertFalse(esp_online())

    def test_attach_never_accepts_other_modules_identity_or_credential(self):
        from unittest.mock import patch

        canonical=module_bridge.CANONICAL_MODULE_ID
        foreign=next(name for name in ("esp","easychange","android","bmollm")
                     if name!=canonical)
        token="T"*48
        with patch.object(module_bridge,"_runtime_token","previous-token"), \
             patch.object(module_bridge,"_runtime_detached",False), \
             patch.object(module_bridge.MCPBridge,"peers",
                          side_effect=AssertionError("FOREIGN_IDENTITY_CONTACTED")) as peers:
            with self.assertRaisesRegex(PermissionError,"HUB_MODULE_IDENTITY_MISMATCH"):
                module_bridge.attach_hub(token,foreign)
            self.assertEqual(module_bridge._runtime_token,"previous-token")
            peers.assert_not_called()

    def test_attach_requires_hub_confirmation_of_own_identity(self):
        from unittest.mock import patch

        canonical=module_bridge.CANONICAL_MODULE_ID
        wrong=next(name for name in ("esp","easychange","android","bmollm")
                   if name!=canonical)
        token="T"*48
        with patch.object(module_bridge,"_runtime_token","previous-token"), \
             patch.object(module_bridge,"_runtime_detached",False), \
             patch.object(module_bridge.MCPBridge,"peers",
                          return_value={"ok":True,"self":wrong}):
            with self.assertRaisesRegex(PermissionError,"HUB_MODULE_IDENTITY_MISMATCH"):
                module_bridge.attach_hub(token,canonical)
            self.assertEqual(module_bridge._runtime_token,"previous-token")
        with patch.object(module_bridge,"_runtime_token",""), \
             patch.object(module_bridge,"_runtime_detached",False), \
             patch.object(module_bridge.MCPBridge,"peers",
                          return_value={"ok":True,"self":canonical}):
            connected=module_bridge.attach_hub(token,canonical)
            self.assertEqual(connected,{"connected":True,"id":canonical})
            self.assertEqual(module_bridge._runtime_token,token)
            self.assertEqual(module_bridge.detach_hub(token),{"connected":False})

    def test_detach_never_recovers_inherited_bootstrap_token(self):
        from unittest.mock import patch

        canonical=module_bridge.CANONICAL_MODULE_ID
        environment_token="E"*48
        active_token="A"*48
        with patch.dict(module_bridge.os.environ,
                        {"MCP_MODULE_BUS_TOKEN":environment_token}), \
             patch.object(module_bridge,"_runtime_token",active_token), \
             patch.object(module_bridge,"_runtime_detached",False), \
             patch.object(module_bridge.MCPBridge,"peers",
                          return_value={"ok":True,"self":canonical}):
            self.assertEqual(module_bridge.MCPBridge().token,active_token)
            self.assertEqual(module_bridge.detach_hub(active_token),
                             {"connected":False})
            self.assertEqual(module_bridge.MCPBridge().token,"")
            self.assertTrue(module_bridge.self_test()["standalone"])
            # Explicit token arguments are still available for a fresh
            # authentication handshake; only implicit env fallback is blocked.
            self.assertEqual(module_bridge.MCPBridge(token=environment_token).token,
                             environment_token)
            new_token="N"*48
            module_bridge.attach_hub(new_token,canonical)
            self.assertEqual(module_bridge.MCPBridge().token,new_token)
            self.assertFalse(module_bridge.self_test()["standalone"])

    def test_bootstrap_credential_can_detach_but_cannot_reactivate_itself(self):
        from unittest.mock import patch

        inherited="B"*48
        with patch.dict(module_bridge.os.environ,
                        {"MCP_MODULE_BUS_TOKEN":inherited}), \
             patch.object(module_bridge,"_runtime_token",""), \
             patch.object(module_bridge,"_runtime_detached",False):
            self.assertEqual(module_bridge.MCPBridge().token,inherited)
            with self.assertRaisesRegex(
                    PermissionError,"INVALID_DETACH_CREDENTIAL"):
                module_bridge.detach_hub("wrong-credential")
            self.assertEqual(module_bridge.MCPBridge().token,inherited)

            self.assertEqual(module_bridge.detach_hub(inherited),
                             {"connected":False})
            self.assertFalse(module_bridge.MCPBridge().connected)
            self.assertTrue(module_bridge.self_test()["standalone"])
            with self.assertRaisesRegex(
                    PermissionError,"INVALID_DETACH_CREDENTIAL"):
                module_bridge.detach_hub(inherited)

            # The bootstrap secret can no longer be implicitly recovered.
            canonical=module_bridge.CANONICAL_MODULE_ID
            fresh="N"*48
            with patch.object(module_bridge.MCPBridge,"peers",
                              return_value={"ok":True,"self":canonical}):
                self.assertEqual(module_bridge.attach_hub(fresh,canonical),
                                 {"connected":True,"id":canonical})
            self.assertEqual(module_bridge.MCPBridge().token,fresh)

    def test_cached_implicit_bridge_is_revoked_after_detach_and_rekey(self):
        from unittest.mock import patch

        bootstrap="B"*48
        canonical=module_bridge.CANONICAL_MODULE_ID
        with patch.dict(module_bridge.os.environ,
                        {"MCP_MODULE_BUS_TOKEN":bootstrap}), \
             patch.object(module_bridge,"_runtime_token",""), \
             patch.object(module_bridge,"_runtime_detached",False):
            cached=module_bridge.MCPBridge()
            self.assertTrue(cached.connected)
            self.assertEqual(cached.token,bootstrap)
            self.assertEqual(module_bridge.detach_hub(bootstrap),
                             {"connected":False})
            self.assertFalse(cached.connected)
            with patch.object(module_bridge._local_opener,"open") as network:
                with self.assertRaisesRegex(
                        RuntimeError,"MCP_NOT_CONNECTED_STANDALONE"):
                    cached.peers()
                network.assert_not_called()

            fresh_token="N"*48
            with patch.object(module_bridge.MCPBridge,"peers",
                              return_value={"ok":True,"self":canonical}):
                module_bridge.attach_hub(fresh_token,canonical)
            self.assertFalse(cached.connected)
            replacement=module_bridge.MCPBridge()
            self.assertTrue(replacement.connected)
            self.assertEqual(replacement.token,fresh_token)

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

    def test_mailbox_send_retries_only_confirmed_unaccepted_backpressure(self):
        import io
        import json
        from unittest.mock import Mock,patch
        from urllib.error import HTTPError

        def rejected(error="MODULE_INBOX_FULL_RETRY_LATER",
                     accepted=False,retryable=True,status=429):
            response={"error":error,"accepted":accepted,"retryable":retryable}
            return HTTPError("http://127.0.0.1:8786/v1/send",status,"busy",{},
                             io.BytesIO(json.dumps(response).encode("utf-8")))

        client=module_bridge.MCPBridge(token="local-test-token")
        success={"accepted":True,"id":37}
        client._request=Mock(side_effect=[rejected(),success])
        with patch("module_bridge.time.sleep") as sleeping:
            result=client.send("esp","job.status",{"ok":True})
        self.assertEqual(result,success)
        self.assertEqual(client._request.call_count,2)
        self.assertEqual(client._request.call_args_list[0],
                         client._request.call_args_list[1])
        sleeping.assert_called_once_with(0.025)

        for response in (rejected(accepted=True),rejected(retryable=False),
                         rejected(error="TOOL_RATE_LIMIT"),
                         rejected(status=503)):
            with self.subTest(response=str(response)):
                client._request=Mock(side_effect=response)
                with patch("module_bridge.time.sleep") as sleeping:
                    with self.assertRaises(HTTPError):
                        client.send("esp","job.status",{"ok":True})
                self.assertEqual(client._request.call_count,1)
                sleeping.assert_not_called()

        client._request=Mock(side_effect=TimeoutError("UNKNOWN_SEND_OUTCOME"))
        with patch("module_bridge.time.sleep") as sleeping:
            with self.assertRaises(TimeoutError):
                client.send("esp","job.status",{"ok":True})
        self.assertEqual(client._request.call_count,1)
        sleeping.assert_not_called()

    def test_mailbox_retries_stop_after_finite_backpressure_budget(self):
        import io
        import json
        from unittest.mock import Mock,patch
        from urllib.error import HTTPError

        def busy():
            return HTTPError(
                "http://127.0.0.1:8786/v1/send",429,"busy",{},
                io.BytesIO(json.dumps({
                    "error":"MODULE_INBOX_FULL_RETRY_LATER",
                    "retryable":True,"accepted":False}).encode("utf-8")))
        client=module_bridge.MCPBridge(token="local-test-token")
        client._request=Mock(side_effect=[busy() for _ in range(7)])
        with patch("module_bridge.time.sleep") as sleeping:
            with self.assertRaises(HTTPError):
                client.send("esp","job.status")
        self.assertEqual(client._request.call_count,7)
        self.assertEqual(sleeping.call_count,6)

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

        maximum=65_536
        oversized=MagicMock()
        oversized.read.return_value=b"x"*(maximum+1)
        oversized.__enter__.return_value=oversized
        with patch.object(module_bridge._local_opener,"open",return_value=oversized):
            with patch.object(module_bridge.json,"loads",
                              side_effect=AssertionError("unbounded parse")):
                with self.assertRaisesRegex(ValueError,"MCP_HUB_RESPONSE_TOO_LARGE"):
                    client.peers()
        oversized.read.assert_called_once_with(maximum+1)

    def test_endpoint_specific_hub_response_read_budgets(self):
        import io
        from unittest.mock import patch

        client=module_bridge.MCPBridge(token="local-test-token")
        scenarios=(
            ("/v1/tools/call",8_388_608+131_072),
            ("/v1/tools",2_097_152),
            ("/v1/inbox",1_048_576),
            ("/v1/inbox/peek",1_048_576),
            ("/v1/send",65_536),
            ("/v1/inbox/ack",65_536),
            ("/v1/peers",65_536),
        )
        for path,maximum in scenarios:
            with self.subTest(endpoint=path):
                reads=[]
                class Tracked(io.BytesIO):
                    def read(self,size=-1):
                        reads.append(size)
                        return super().read(size)
                healthy=Tracked(b'{"ok":true}')
                with patch.object(module_bridge._local_opener,
                                  "open",return_value=healthy):
                    self.assertEqual(client._request(path),{"ok":True})
                self.assertEqual(reads,[maximum+1])

    def test_hub_response_rejects_non_object_json(self):
        import io
        from unittest.mock import patch
        client=module_bridge.MCPBridge(token="local-test-token")
        with patch.object(module_bridge._local_opener,"open",
                          return_value=io.BytesIO(b'[1,2,3]')):
            with self.assertRaisesRegex(ValueError,"INVALID_MCP_HUB_RESPONSE"):
                client.peers()

    def test_module_bridge_rejects_oversized_http_before_network(self):
        import io
        from unittest.mock import patch

        bridge=module_bridge.MCPBridge(token="local-test-token")
        with patch.object(module_bridge._local_opener,"open") as network:
            with self.assertRaisesRegex(ValueError,"MCP_BUS_REQUEST_TOO_LARGE"):
                bridge._request("/v1/send",{"payload":"x"*18000})
            with self.assertRaisesRegex(ValueError,"MCP_BUS_REQUEST_TOO_LARGE"):
                bridge._request("/v1/inbox/ack",{"ids":["x"*18000]})
            with self.assertRaisesRegex(ValueError,"MCP_BUS_REQUEST_TOO_LARGE"):
                bridge._request("/v1/tools/call",
                                {"arguments":{"value":"x"*1_048_577}})
        network.assert_not_called()

        # Tool RPCs have a larger contract than mailbox/ACK control calls.
        with patch.object(module_bridge._local_opener,"open",
                          return_value=io.BytesIO(b'{"ok":true}')) as network:
            self.assertEqual(
                bridge._request("/v1/tools/call",
                                {"arguments":{"value":"x"*20000}}),
                {"ok":True})
        self.assertGreater(len(network.call_args.args[0].data),17408)


    def test_hub_never_serializes_nonfinite_json_or_calls_network(self):
        import math
        from unittest.mock import patch

        bridge=module_bridge.MCPBridge(token="local-test-credential")
        with patch.object(module_bridge._local_opener,"open") as network:
            for invalid in (math.nan,math.inf,-math.inf):
                with self.subTest(number=repr(invalid)):
                    with self.assertRaisesRegex(ValueError,
                                                "Out of range float values"):
                        bridge.send("esp","test.nonfinite",
                                    {"nested":{"bad":invalid}})
                    with self.assertRaisesRegex(ValueError,
                                                "Out of range float values"):
                        bridge.call("read_only",value=[{"n":invalid}])
                    with self.assertRaisesRegex(ValueError,
                                                "Out of range float values"):
                        bridge.ack([invalid])
            network.assert_not_called()

    def test_hub_reply_rejects_nonfinite_json_before_use(self):
        import io
        from unittest.mock import patch

        bridge=module_bridge.MCPBridge(token="local-test-credential")
        for number in ("NaN","Infinity","-Infinity","1e100000"):
            with self.subTest(number=number):
                response=io.BytesIO(
                    ('{"ok":true,"self":"'+module_bridge.CANONICAL_MODULE_ID+
                     '","metrics":{"value":'+number+'}}').encode("ascii"))
                with patch.object(module_bridge._local_opener,"open",
                                  return_value=response) as network:
                    with self.assertRaisesRegex(
                            ValueError,"MCP_HUB_NONFINITE_JSON"):
                        bridge.peers()
                    network.assert_called_once()

        good=io.BytesIO(b'{"ok":true,"metrics":{"value":2.5}}')
        with patch.object(module_bridge._local_opener,"open",
                          return_value=good):
            self.assertEqual(bridge.peers()["metrics"]["value"],2.5)

    def test_outgoing_bridge_rejects_ambiguous_json_keys_before_network(self):
        from unittest.mock import patch

        client=module_bridge.MCPBridge(token="bridge-test-token")
        invalid=(
            {1:"number","1":"text"},
            {"nested":[{False:"boolean","false":"string"}]},
            [{"sub":{"name":"ok",None:"null-key"}}],
        )
        with patch("module_bridge._local_opener.open") as request:
            for payload in invalid:
                with self.subTest(value=repr(payload)):
                    with self.assertRaisesRegex(ValueError,
                                                "MCP_BUS_INVALID_JSON_KEY"):
                        client.send("mcp","test.keys",payload,
                                    correlation_id="duplicate-key")
                    with self.assertRaisesRegex(ValueError,
                                                "MCP_BUS_INVALID_JSON_KEY"):
                        client.call("mcp_core_status",payload=payload)
            request.assert_not_called()

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
