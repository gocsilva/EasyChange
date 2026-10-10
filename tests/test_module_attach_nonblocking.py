"""Slow hub identity probes must not hold the local credential lifecycle lock.

These tests use mocked authenticated replies; no MCP server or device required.
"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import module_bridge as bridge


class AttachConcurrencyTests(unittest.TestCase):
    def _isolated(self):
        return (
            patch.object(bridge, "_runtime_token", "A" * 48),
            patch.object(bridge, "_runtime_detached", False),
            patch.object(bridge, "_runtime_state_epoch", 0),
        )

    def test_detach_preempts_inflight_attach_without_waiting_for_http(self):
        entered=threading.Event()
        release=threading.Event()
        outcomes=[]
        canonical=bridge.CANONICAL_MODULE_ID

        def slow_peers(*_args, **_kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("simulated peer request exceeded deadline")
            return {"ok":True,"self":canonical}

        def attach():
            try:
                outcomes.append(bridge.attach_hub("B"*48,canonical))
            except Exception as exc:
                outcomes.append(exc)

        locks=self._isolated()
        with locks[0],locks[1],locks[2], \
             patch.object(bridge.MCPBridge,"peers",side_effect=slow_peers):
            thread=threading.Thread(target=attach,daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                # The peer probe is deliberately still blocked. Detach must
                # acquire the lifecycle lock immediately, not await HTTP.
                self.assertTrue(bridge._runtime_state_lock.acquire(blocking=False))
                bridge._runtime_state_lock.release()
                self.assertEqual(bridge.detach_hub("A"*48),{"connected":False})
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcomes),1)
            self.assertIsInstance(outcomes[0],RuntimeError)
            self.assertEqual(str(outcomes[0]),"HUB_ATTACH_SUPERSEDED")
            self.assertEqual(bridge._runtime_token,"")
            self.assertTrue(bridge._runtime_detached)

    def test_concurrent_identical_verified_attaches_are_idempotent(self):
        entered=threading.Event()
        release=threading.Event()
        outcomes=[]
        canonical=bridge.CANONICAL_MODULE_ID
        token="B"*48

        def peers(*_args, **_kwargs):
            if threading.current_thread().name=="slow-hub-proof":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("simulated peer request exceeded deadline")
            return {"ok":True,"self":canonical}

        def slow():
            try:
                outcomes.append(bridge.attach_hub(token,canonical))
            except Exception as exc:
                outcomes.append(exc)

        locks=self._isolated()
        with locks[0],locks[1],locks[2], \
             patch.object(bridge.MCPBridge,"peers",side_effect=peers):
            thread=threading.Thread(target=slow,name="slow-hub-proof",daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(bridge.attach_hub(token,canonical),
                                 {"connected":True,"id":canonical})
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes,[{"connected":True,"id":canonical}])
            self.assertEqual(bridge._runtime_token,token)
            self.assertEqual(bridge._runtime_state_epoch,1)


    def test_newer_verified_token_preempts_slower_different_token(self):
        entered=threading.Event()
        release=threading.Event()
        outcomes=[]
        canonical=bridge.CANONICAL_MODULE_ID
        slower="B"*48
        newer="C"*48

        def peers(*_args, **_kwargs):
            if threading.current_thread().name=="older-attach":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("simulated peer request exceeded deadline")
            return {"ok":True,"self":canonical}

        def delayed():
            try:
                outcomes.append(bridge.attach_hub(slower,canonical))
            except Exception as exc:
                outcomes.append(exc)

        locks=self._isolated()
        with locks[0],locks[1],locks[2], \
             patch.object(bridge.MCPBridge,"peers",side_effect=peers):
            thread=threading.Thread(target=delayed,name="older-attach",daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(bridge.attach_hub(newer,canonical),
                                 {"connected":True,"id":canonical})
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcomes),1)
            self.assertIsInstance(outcomes[0],RuntimeError)
            self.assertEqual(str(outcomes[0]),"HUB_ATTACH_SUPERSEDED")
            self.assertEqual(bridge._runtime_token,newer)
            self.assertFalse(bridge._runtime_detached)
            self.assertEqual(bridge._runtime_state_epoch,1)



    def test_attach_probe_budget_rejects_saturation_without_probing_hub(self):
        from unittest.mock import patch
        canonical=bridge.CANONICAL_MODULE_ID
        limit=threading.BoundedSemaphore(4)
        for _ in range(4):
            self.assertTrue(limit.acquire(blocking=False))
        try:
            guards=self._isolated()
            with guards[0],guards[1],guards[2], \
                 patch.object(bridge,"_attach_probe_slots",limit), \
                 patch.object(bridge.MCPBridge,"peers") as peers:
                with self.assertRaisesRegex(RuntimeError,"HUB_ATTACH_BUSY"):
                    bridge.attach_hub("N"*48,canonical)
                peers.assert_not_called()
                self.assertEqual(bridge._runtime_token,"A"*48)
                self.assertFalse(bridge._runtime_detached)
                self.assertEqual(bridge._runtime_state_epoch,0)
        finally:
            for _ in range(4):
                limit.release()



if __name__=="__main__":
    unittest.main()
