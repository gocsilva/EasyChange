"""Admission backpressure is replayable only with a complete bounded 429 proof."""
import io
import json
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError

import module_bridge


class ReadRecorder(io.BytesIO):
    def __init__(self,content):
        super().__init__(content)
        self.limits=[]
    def read(self,limit=-1):
        self.limits.append(limit)
        return super().read(limit)


class BackpressureFramingTests(unittest.TestCase):
    def rejection(self,reason,*,extra=b""):
        encoded=json.dumps({"error":reason,"accepted":False,"retryable":True},
                           separators=(",",":")).encode("utf-8")
        if extra:
            # First 2048 bytes are syntactically valid JSON followed by
            # whitespace, so reading only 2048 produces a false safe-retry.
            self.assertLess(len(encoded),2048)
            encoded=encoded+b" "*(2048-len(encoded))+extra
        body=ReadRecorder(encoded)
        error=HTTPError("http://127.0.0.1:8786/v1/send",429,
                        "admission denied",{},body)
        return error,body

    def test_rejected_rpc_with_trailing_unread_bytes_is_not_replayed(self):
        error,body=self.rejection("MODULE_TOOL_RPC_BUSY_RETRY_LATER",
                                   extra=b"UNTRUSTED")
        bridge=module_bridge.MCPBridge(token="local-test-token")
        bridge._request=Mock(side_effect=[error,{"ok":True}])
        with patch("module_bridge.time.sleep") as delay:
            with self.assertRaises(HTTPError):
                bridge.call("possibly_mutating_operation",arg=1)
        bridge._request.assert_called_once()
        delay.assert_not_called()
        self.assertEqual(body.limits,[2049])

    def test_rejected_send_with_trailing_unread_bytes_is_not_replayed(self):
        error,body=self.rejection("MODULE_INBOX_FULL_RETRY_LATER",
                                   extra=b"UNTRUSTED")
        bridge=module_bridge.MCPBridge(token="local-test-token")
        bridge._request=Mock(side_effect=[error,{"accepted":True}])
        with patch("module_bridge.time.sleep") as delay:
            with self.assertRaises(HTTPError):
                bridge.send("esp","event",[1])
        bridge._request.assert_called_once()
        delay.assert_not_called()
        self.assertEqual(body.limits,[2049])

    def test_complete_small_backpressure_proof_remains_retryable(self):
        for operation,reason in (("call","MODULE_TOOL_RPC_BUSY_RETRY_LATER"),
                                 ("send","MODULE_INBOX_FULL_RETRY_LATER")):
            with self.subTest(operation=operation):
                error,body=self.rejection(reason)
                bridge=module_bridge.MCPBridge(token="local-test-token")
                response={"ok":True,"accepted":True}
                bridge._request=Mock(side_effect=[error,response])
                with patch("module_bridge.time.sleep") as delay:
                    actual=(bridge.call("read_only") if operation=="call"
                            else bridge.send("esp","event",{"ok":True}))
                self.assertEqual(actual,response)
                self.assertEqual(bridge._request.call_count,2)
                self.assertEqual(body.limits,[2049])
                delay.assert_called_once_with(0.025)


if __name__=="__main__":
    unittest.main()
