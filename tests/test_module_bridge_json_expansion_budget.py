"""Bound JSON encoding before repeated shared references expand into huge payloads."""
from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

import module_bridge


class SharedGraphBudgetTests(unittest.TestCase):
    def bridge(self):
        return module_bridge.MCPBridge(token="unit-test-local-credential")

    def test_shared_dag_does_not_materialize_exponential_json(self):
        shared={"value":"payload"}
        for _ in range(30):
            shared=[shared,shared]
        client=self.bridge()
        with patch.object(module_bridge.json,"dumps",
                          side_effect=AssertionError("UNBOUNDED_JSON_DUMPS")) as dumps, \
             patch.object(module_bridge._local_opener,"open") as opener:
            with self.assertRaisesRegex(ValueError,"MCP_BUS_REQUEST_TOO_LARGE"):
                client.send("mcp","large.shared",shared)
        dumps.assert_not_called()
        opener.assert_not_called()

    def test_valid_shared_reference_is_serialized_twice_as_normal_json(self):
        shared={"value":"small","emoji":"🌱"}
        client=self.bridge()
        with patch.object(module_bridge._local_opener,"open",
                          return_value=io.BytesIO(b'{"ok":true}')) as opener:
            response=client.send("mcp","small.shared",
                                 {"first":shared,"second":shared})
        self.assertIs(response["ok"],True)
        request=opener.call_args.args[0]
        sent=json.loads(request.data)
        self.assertEqual(sent["payload"]["first"],shared)
        self.assertEqual(sent["payload"]["second"],shared)

    def test_utf8_bytes_not_character_count_control_limit(self):
        client=self.bridge()
        with patch.object(module_bridge._local_opener,"open") as opener:
            with self.assertRaisesRegex(ValueError,"MCP_BUS_REQUEST_TOO_LARGE"):
                client.send("mcp","utf8.large",{"text":"🌱"*5000})
        opener.assert_not_called()

    def test_small_utf8_payload_is_allowed(self):
        client=self.bridge()
        with patch.object(module_bridge._local_opener,"open",
                          return_value=io.BytesIO(b'{"ok":true}')) as opener:
            self.assertTrue(client.send("mcp","utf8.small",
                                        {"text":"🌱"*100})["ok"])
        opener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
