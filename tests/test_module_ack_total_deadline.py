"""A recipient lease must not turn ACK retry into three full hub timeouts.

All cases use an in-memory virtual clock; no HTTP, MCP server, serial device,
remote computer or real waiting is involved.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from module_bridge import MCPBridge


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ACKDeadlineTests(unittest.TestCase):
    def test_one_full_hub_timeout_exhausts_total_budget(self):
        clock = Clock()
        bridge = MCPBridge(token="synthetic-local-test-token")
        calls = []

        def stalled(path, payload, *, timeout):
            calls.append((path, dict(payload), timeout))
            clock.now += 8.01
            raise TimeoutError("ACK_RESPONSE_UNKNOWN")

        with patch("module_bridge.time", SimpleNamespace(
                monotonic=clock.monotonic, sleep=clock.sleep)), \
             patch.object(bridge, "_request", side_effect=stalled):
            with self.assertRaises(TimeoutError):
                bridge.ack([41])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("/v1/inbox/ack", {"ids": [41]}, 8.0))
        self.assertEqual(clock.sleeps, [])

    def test_remaining_budget_is_passed_to_second_attempt(self):
        clock = Clock()
        bridge = MCPBridge(token="synthetic-local-test-token")
        timeouts = []

        def late_failure(path, payload, *, timeout):
            timeouts.append(timeout)
            clock.now += 7.90 if len(timeouts) == 1 else .25
            raise TimeoutError("ACK_RESPONSE_LOST")

        with patch("module_bridge.time", SimpleNamespace(
                monotonic=clock.monotonic, sleep=clock.sleep)), \
             patch.object(bridge, "_request", side_effect=late_failure):
            with self.assertRaises(TimeoutError):
                bridge.ack([42])

        self.assertEqual(len(timeouts), 2)
        self.assertEqual(timeouts[0], 8.0)
        self.assertGreater(timeouts[1], 0)
        self.assertLess(timeouts[1], .101)
        self.assertEqual(len(clock.sleeps), 1)

    def test_fast_lost_ack_retries_same_ids_with_success(self):
        clock = Clock()
        bridge = MCPBridge(token="synthetic-local-test-token")
        requests = []
        expected = {"ok": True, "acked": 0, "already_acked": [43],
                    "unknown": []}

        def lost_then_found(path, payload, *, timeout):
            requests.append((path, dict(payload), timeout))
            if len(requests) == 1:
                clock.now += .20
                raise ConnectionError("ACK_RECEIPT_LOST")
            return expected

        with patch("module_bridge.time", SimpleNamespace(
                monotonic=clock.monotonic, sleep=clock.sleep)), \
             patch.object(bridge, "_request", side_effect=lost_then_found):
            self.assertEqual(bridge.ack([43]), expected)

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][:2], requests[1][:2])
        self.assertEqual(requests[0][2], 8.0)
        self.assertLess(requests[1][2], 8.0)
        self.assertEqual(clock.sleeps, [.05])


if __name__ == "__main__":
    unittest.main()
