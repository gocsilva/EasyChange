"""MCP credentials must never be forwarded outside the configured loopback hub."""
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.error import HTTPError
import module_bridge

class TestHubRedirectBoundary(unittest.TestCase):
    def test_hub_rejections_close_http_error_response(self):
        import io
        from urllib.error import HTTPError
        from unittest.mock import Mock, patch
        from mcp_adapter.module_service import Handler

        for upstream, expected in ((403, 403),(503, 503)):
            with self.subTest(status=upstream):
                payload=b'{"token":"invalid-test-token"}'
                handler=object.__new__(Handler)
                handler.path="/v1/attach"
                handler.headers={"Content-Type":"application/json",
                                 "Content-Length":str(len(payload))}
                handler.rfile=io.BytesIO(payload)
                handler.send=Mock()
                response=io.BytesIO(b'{"error":"UNTRUSTED_UPSTREAM_CONTENT"}')
                failure=HTTPError("http://127.0.0.1:8786/v1/peers",
                                  upstream,"hub rejected",{},response)
                with patch("module_bridge.attach_hub",side_effect=failure):
                    handler.do_POST()
                self.assertTrue(response.closed)
                label=("HUB_CREDENTIAL_REJECTED" if expected==403
                       else "HUB_UNAVAILABLE")
                handler.send.assert_called_once_with(expected,{"error":label})


    def test_offline_hub_attach_returns_recoverable_http_503(self):
        import io
        from unittest.mock import Mock,patch
        from mcp_adapter.module_service import Handler

        for exc in (OSError("SECRET_CONNECTION_STRING"),
                    RuntimeError("MCP_HOST_UNAVAILABLE")):
            with self.subTest(exc=type(exc).__name__):
                handler=object.__new__(Handler)
                handler.path="/v1/attach"
                payload=b'{"token":"test-private-bearer"}'
                handler.headers={"Content-Type":"application/json",
                                 "Content-Length":str(len(payload))}
                handler.rfile=io.BytesIO(payload)
                handler.send=Mock()
                with patch("module_bridge.attach_hub",side_effect=exc):
                    handler.do_POST()
                handler.send.assert_called_once_with(
                    503,{"error":"HUB_UNAVAILABLE"})
                self.assertNotIn("SECRET_CONNECTION_STRING",
                                 str(handler.send.call_args))

        handler=object.__new__(Handler)
        handler.path="/v1/attach"
        payload=b'{"token":"bad-private-bearer"}'
        handler.headers={"Content-Type":"application/json",
                         "Content-Length":str(len(payload))}
        handler.rfile=io.BytesIO(payload)
        handler.send=Mock()
        with patch("module_bridge.attach_hub",
                   side_effect=PermissionError("INVALID_DETACH_CREDENTIAL")):
            handler.do_POST()
        handler.send.assert_called_once_with(
            403,{"error":"PermissionError"})

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

    def test_local_peer_health_rejects_nan_and_overflow_json(self):
        import io
        from unittest.mock import patch
        from mcp_adapter.module_service import esp_online

        for raw in (
            b'{"ok":true,"id":"esp","unused":NaN}',
            b'{"ok":true,"id":"esp","unused":Infinity}',
            b'{"ok":true,"id":"esp","unused":1e999}',
        ):
            with self.subTest(raw=raw), patch(
                    "mcp_adapter.module_service.urlopen",
                    return_value=io.BytesIO(raw)):
                self.assertFalse(esp_online())
        with patch("mcp_adapter.module_service.urlopen",
                   return_value=io.BytesIO(b'{"ok":true,"id":"esp","unused":1.5}')):
            self.assertTrue(esp_online())

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
            ("/v1/inbox",2_097_152),
            ("/v1/inbox/peek",2_097_152),
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

    def test_full_hundred_message_inbox_fits_bounded_hub_response(self):
        import io
        import json
        from unittest.mock import patch

        client=module_bridge.MCPBridge(token="local-test-token")
        # The server accepts 100 envelopes carrying <=16 KiB JSON payloads.
        # Their aggregate size legitimately exceeds the old 1 MiB reader cap.
        messages=[{"id":i+1,"source":"bmollm","target":"esp",
                   "topic":"bulk.sample","payload":"X"*16000}
                  for i in range(100)]
        wire=json.dumps({"ok":True,"messages":messages},separators=(",",":")).encode()
        self.assertGreater(len(wire),1_048_576)
        self.assertLess(len(wire),2_097_152)
        with patch.object(module_bridge._local_opener,"open",
                          return_value=io.BytesIO(wire)):
            decoded=client._request("/v1/inbox/peek")
        self.assertEqual(len(decoded["messages"]),100)
        self.assertEqual(decoded["messages"][-1]["id"],100)

    def test_inbox_ack_recovers_lost_reply_without_reprocessing_handler(self):
        from unittest.mock import Mock, patch
        from urllib.error import URLError

        client=module_bridge.MCPBridge(token="local-test-token")
        accepted={"ok":True,"recipient":module_bridge.CANONICAL_MODULE_ID,
                  "acked":0,"acked_ids":[],"already_acked":[17],"unknown":[]}
        client._request=Mock(side_effect=[
            TimeoutError("ACK_REPLY_LOST"),accepted])
        with patch("module_bridge.time.sleep") as sleep:
            reply=client.ack([17])
        self.assertEqual(reply,accepted)
        self.assertEqual(client._request.call_count,2)
        self.assertEqual(client._request.call_args_list[0],
                         client._request.call_args_list[1])
        sleep.assert_called_once_with(0.05)

        client._request=Mock(side_effect=[
            URLError("CONNECTION_RESET"),URLError("CONNECTION_RESET"),accepted])
        with patch("module_bridge.time.sleep") as sleep:
            self.assertEqual(client.ack([17]),accepted)
        self.assertEqual(client._request.call_count,3)
        self.assertEqual(sleep.call_count,2)

    def test_inbox_ack_never_retries_http_rejection_or_invalid_receipt(self):
        import io
        from unittest.mock import Mock, patch
        from urllib.error import HTTPError

        client=module_bridge.MCPBridge(token="local-test-token")
        unauthorized=HTTPError("http://127.0.0.1:8786/v1/inbox/ack",
                               403,"forbidden",{},
                               io.BytesIO(b'{"error":"BEARER_REQUIRED"}'))
        for failure in (unauthorized, ValueError("INVALID_MCP_HUB_RESPONSE")):
            with self.subTest(failure=type(failure).__name__):
                client._request=Mock(side_effect=failure)
                with patch("module_bridge.time.sleep") as sleep:
                    with self.assertRaises(type(failure)):
                        client.ack([17])
                self.assertEqual(client._request.call_count,1)
                sleep.assert_not_called()

        client._request=Mock(side_effect=TimeoutError("ACK_UNCERTAIN"))
        with patch("module_bridge.time.sleep") as sleep:
            with self.assertRaises(TimeoutError):
                client.ack([17])
        self.assertEqual(client._request.call_count,3)
        self.assertEqual(sleep.call_count,2)

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

    def test_backpressure_response_socket_is_closed_before_retry(self):
        import io
        import json
        from unittest.mock import Mock,patch
        from urllib.error import HTTPError

        client=module_bridge.MCPBridge(token="connection-lifetime-test")
        operations=(
            (lambda: client.send("mcp","test.busy",{"safe":True}),
             "MODULE_INBOX_FULL_RETRY_LATER"),
            (lambda: client.call("mcp_core_status"),
             "MODULE_TOOL_RPC_BUSY_RETRY_LATER"),
        )
        for action,error in operations:
            with self.subTest(error=error):
                body=io.BytesIO(json.dumps({
                    "error":error,"retryable":True,"accepted":False
                }).encode("utf-8"))
                busy=HTTPError("http://127.0.0.1:8786/v1/send",429,
                               "busy",{},body)
                client._request=Mock(side_effect=[busy,{"ok":True}])
                def verify_closed_before_backoff(_seconds):
                    self.assertTrue(body.closed)
                with patch("module_bridge.time.sleep",
                           side_effect=verify_closed_before_backoff) as sleep:
                    self.assertEqual(action(),{"ok":True})
                self.assertEqual(client._request.call_count,2)
                sleep.assert_called_once()
                self.assertTrue(body.closed)

    def test_shared_json_subtrees_expand_correctly_and_cycles_fail_closed(self):
        import io
        import json
        from unittest.mock import patch

        bridge=module_bridge.MCPBridge(token="test-bridge-token")
        shared={"parts":[{"v":"á"},1,True]}
        payload={"tool":"test.tool","arguments":{"copies":[shared]*80}}
        with patch.object(module_bridge._local_opener,"open",
                          return_value=io.BytesIO(b'{"ok":true}')) as opener:
            self.assertEqual(bridge._request("/v1/tools/call",payload),
                             {"ok":True})
        outbound=json.loads(opener.call_args.args[0].data)
        self.assertEqual(outbound["arguments"]["copies"],[shared]*80)
        self.assertEqual(len(outbound["arguments"]["copies"]),80)

        cycle={"item":"unsafe"}
        cycle["self"]=cycle
        with patch.object(module_bridge._local_opener,"open",
                          side_effect=AssertionError("CYCLE_SENT_OVER_NETWORK")) as opener:
            with self.assertRaises((ValueError,RecursionError)):
                bridge._request("/v1/send",{"payload":cycle})
            opener.assert_not_called()

    def test_json_encoder_limits_unicode_fragment_memory(self):
        import io
        import json
        from unittest.mock import patch

        sizes=[]
        class Tracked(str):
            def __getitem__(self, key):
                return Tracked(super().__getitem__(key))
            def encode(self, encoding="utf-8", errors="strict"):
                sizes.append(len(self))
                if len(self)>4096:
                    raise AssertionError("UTF8_ENCODE_UNBOUNDED_FRAGMENT")
                return super().encode(encoding, errors)
        class StubEncoder:
            def __init__(self,*args,**kwargs):
                pass
            def iterencode(self,_):
                yield Tracked("界"*9000)

        bridge=module_bridge.MCPBridge(token="local-test-token")
        with patch.object(module_bridge.json,"JSONEncoder",StubEncoder), \
             patch.object(module_bridge._local_opener,"open",
                          side_effect=AssertionError("NETWORK_BEFORE_BYTE_BUDGET")) as opener:
            with self.assertRaisesRegex(ValueError,"MCP_BUS_REQUEST_TOO_LARGE"):
                bridge._request("/v1/send",{"payload":"placeholder"})
            opener.assert_not_called()
        self.assertGreater(len(sizes),1)
        self.assertLessEqual(max(sizes),4096)

        data={"topic":"status.ok","payload":{"message":"界"*1000}}
        with patch.object(module_bridge._local_opener,"open",
                          return_value=io.BytesIO(b'{"ok":true}')) as opener:
            self.assertEqual(bridge._request("/v1/send",data),{"ok":True})
        request=opener.call_args.args[0]
        self.assertEqual(json.loads(request.data),data)
        self.assertEqual(request.data,json.dumps(
            data,ensure_ascii=False,allow_nan=False).encode("utf-8"))

    def test_peer_health_rejection_closes_http_response(self):
        import io
        from urllib.error import HTTPError
        from unittest.mock import patch
        import mcp_adapter.module_service as service

        for status in (401,403,429,503):
            with self.subTest(code=status):
                stream=io.BytesIO(b"untrusted local peer error")
                rejected=HTTPError(
                    "http://127.0.0.1/health", status,
                    "service rejected",{},stream)
                with patch("mcp_adapter.module_service.urlopen",side_effect=rejected):
                    self.assertFalse(service.esp_online())
                self.assertTrue(stream.closed)

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
