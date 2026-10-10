"""Opt-in module message processing must never ACK before success."""
from __future__ import annotations
import unittest
from unittest.mock import Mock
from module_bridge import MCPBridge

def item(ident):
    return {"id":ident,"source":"mcp","target":"unit","topic":"unit.test",
            "payload":{"number":ident},"correlation_id":"cid","created_at":0}

class TestProcessPending(unittest.TestCase):
    def bridge(self,messages):
        b=MCPBridge(token="local-test-credential")
        b.receive=Mock(return_value={"messages":messages,"pending":len(messages),
                                     "requires_ack":True})
        b.ack=Mock(side_effect=lambda ids:{"ok":True,"acked":len(ids),
                            "already_acked":[],"unknown":[]})
        return b

    def test_only_explicit_true_is_success(self):
        for result in (None,False,1,"ok",{}):
            with self.subTest(result=result):
                b=self.bridge([item(1)])
                report=b.process_pending(lambda msg:result)
                self.assertEqual(report["acknowledged"],0)
                self.assertEqual(report["deferred_id"],1)
                b.ack.assert_not_called()

    def test_processes_each_message_then_acks_individually(self):
        b=self.bridge([item(2),item(3)])
        observed=[]
        report=b.process_pending(lambda message: observed.append(message["id"]) or True)
        self.assertEqual(observed,[2,3])
        self.assertEqual(report["acknowledged"],2)
        self.assertIsNone(report["deferred_id"])
        self.assertEqual(b.ack.call_args_list,[unittest.mock.call([2]),
                                               unittest.mock.call([3])])

    def test_failed_handler_stops_without_acking_failed_or_later_work(self):
        b=self.bridge([item(1),item(2),item(3)])
        observed=[]
        def handler(message):
            observed.append(message["id"])
            return message["id"]==1
        report=b.process_pending(handler)
        self.assertEqual(observed,[1,2])
        self.assertEqual(report["acknowledged"],1)
        self.assertEqual(report["deferred_id"],2)
        b.ack.assert_called_once_with([1])

    def test_exception_retains_message_for_retry(self):
        b=self.bridge([item(4)])
        def fail(_):
            raise RuntimeError("TASK_FAILED")
        with self.assertRaisesRegex(RuntimeError,"TASK_FAILED"):
            b.process_pending(fail)
        b.ack.assert_not_called()

    def test_ack_rejection_propagates_and_stops_processing(self):
        b=self.bridge([item(5),item(6)])
        b.ack.return_value={"ok":True,"acked":0,"already_acked":[],"unknown":[5]}
        b.ack.side_effect=None
        with self.assertRaisesRegex(RuntimeError,"MCP_ACK_UNCONFIRMED"):
            b.process_pending(lambda _message:True)
        b.ack.assert_called_once_with([5])

    def test_lost_ack_response_propagates_uncertainty(self):
        b=self.bridge([item(7)])
        b.ack.side_effect=TimeoutError("ACK_RESPONSE_LOST")
        with self.assertRaisesRegex(TimeoutError,"ACK_RESPONSE_LOST"):
            b.process_pending(lambda _:True)

    def test_bad_message_structure_fails_closed(self):
        b=self.bridge([{"id":"7","topic":"test"}])
        with self.assertRaisesRegex(ValueError,"INVALID_MCP_INBOX_MESSAGE"):
            b.process_pending(lambda _:True)
        b.ack.assert_not_called()

    def test_rejects_invalid_handler_and_limit_without_network(self):
        b=self.bridge([item(1)])
        for invalid in (None,123,True):
            with self.assertRaisesRegex(ValueError,"HANDLER_REQUIRED"):
                b.process_pending(invalid)
        for invalid in (0,101,True,1.5):
            with self.assertRaisesRegex(ValueError,"INVALID_LIMIT"):
                b.process_pending(lambda _:True,limit=invalid)
        b.receive.assert_not_called()

    def test_concurrent_process_calls_cannot_double_execute_same_mailbox(self):
        import threading
        first=self.bridge([item(31)])
        second=self.bridge([item(31)])
        started=threading.Event()
        release=threading.Event()
        finished=[]
        def handler(_):
            started.set()
            if not release.wait(timeout=4):
                raise TimeoutError("TEST_HANDLER_WAIT_EXPIRED")
            return True
        def worker():
            try:
                finished.append(("ok",first.process_pending(handler)))
            except Exception as exc:
                finished.append(("error",str(exc)))
        thread=threading.Thread(target=worker,daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(timeout=2))
            with self.assertRaisesRegex(RuntimeError,"MCP_CONSUMER_BUSY"):
                second.process_pending(lambda _:True)
            second.ack.assert_not_called()
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(finished[0][0],"ok")
        self.assertEqual(first.ack.call_count,1)
        # Lock must be reusable after the callback exits.
        self.assertEqual(second.process_pending(lambda _:True)["acknowledged"],1)

    def test_consumer_lock_released_after_handler_exception(self):
        b=self.bridge([item(41)])
        def fail(_):
            raise RuntimeError("HANDLER_CRASH")
        with self.assertRaisesRegex(RuntimeError,"HANDLER_CRASH"):
            b.process_pending(fail)
        self.assertEqual(b.process_pending(lambda _:True)["acknowledged"],1)

    def test_duplicate_ids_fail_closed_before_any_handler_runs(self):
        b=self.bridge([item(1),item(1)])
        handler=Mock(return_value=True)
        with self.assertRaisesRegex(ValueError,"DUPLICATE_MCP_INBOX_ID"):
            b.process_pending(handler)
        handler.assert_not_called()
        b.ack.assert_not_called()

    def test_malformed_second_entry_fails_before_first_side_effect(self):
        b=self.bridge([item(1),{"id":"bad"}])
        handler=Mock(return_value=True)
        with self.assertRaisesRegex(ValueError,"INVALID_MCP_INBOX_MESSAGE"):
            b.process_pending(handler)
        handler.assert_not_called()
        b.ack.assert_not_called()

    def test_inconsistent_ack_receipt_fails_closed(self):
        b=self.bridge([item(1)])
        b.ack.side_effect=None
        for reply in ({"ok":True,"acked":True,"unknown":[]},
                      {"ok":True,"acked":1,"unknown":[1]},
                      {"ok":True,"acked":1,"already_acked":[1]},
                      {"ok":True,"acked":0,"already_acked":[1],"unknown":[1]}):
            with self.subTest(reply=reply):
                b.ack.return_value=reply
                with self.assertRaisesRegex(RuntimeError,"MCP_ACK_UNCONFIRMED"):
                    b.process_pending(lambda _:True)

    def test_reentrant_processing_on_same_client_is_rejected(self):
        import threading
        entered=threading.Event()
        release=threading.Event()
        b=self.bridge([item(1)])
        b.ack.side_effect=None
        b.ack.return_value={"ok":True,"acked":1,"already_acked":[],"unknown":[]}
        outcomes=[]
        def handler(_):
            entered.set()
            if not release.wait(timeout=4):
                raise TimeoutError("TEST_HANDLER_STALLED")
            return True
        def worker():
            try:
                outcomes.append(b.process_pending(handler))
            except Exception as exc:
                outcomes.append(exc)
        thread=threading.Thread(target=worker,daemon=True)
        thread.start()
        self.assertTrue(entered.wait(timeout=2))
        try:
            with self.assertRaisesRegex(RuntimeError,"MCP_CONSUMER_BUSY"):
                b.process_pending(handler)
        finally:
            release.set()
            thread.join(timeout=4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes),1)
        self.assertEqual(outcomes[0]["acknowledged"],1)
        b.ack.assert_called_once_with([1])
        b.receive.assert_called_once()

    def test_opt_in_batch_ack_combines_only_successful_messages(self):
        b=self.bridge([item(i) for i in range(1,7)])
        result=b.process_pending(lambda _:True,ack_batch_size=4)
        self.assertEqual(result["acknowledged"],6)
        self.assertEqual(b.ack.call_args_list,[
            unittest.mock.call([1,2,3,4]),unittest.mock.call([5,6])])

    def test_opt_in_batch_ack_flushes_before_deferred_item(self):
        b=self.bridge([item(i) for i in range(1,7)])
        observed=[]
        def handler(msg):
            observed.append(msg["id"])
            return msg["id"]!=4
        result=b.process_pending(handler,ack_batch_size=5)
        self.assertEqual(observed,[1,2,3,4])
        self.assertEqual(result["acknowledged"],3)
        self.assertEqual(result["deferred_id"],4)
        b.ack.assert_called_once_with([1,2,3])

    def test_opt_in_batch_ack_flushes_completed_work_on_callback_exception(self):
        b=self.bridge([item(i) for i in range(1,5)])
        def handler(msg):
            if msg["id"]==3: raise RuntimeError("BATCH_HANDLER_FAILED")
            return True
        with self.assertRaisesRegex(RuntimeError,"BATCH_HANDLER_FAILED"):
            b.process_pending(handler,ack_batch_size=4)
        b.ack.assert_called_once_with([1,2])

    def test_opt_in_batch_ack_strictly_validates_receipt(self):
        b=self.bridge([item(1),item(2)])
        b.ack.side_effect=None
        b.ack.return_value={"ok":True,"acked":2,
                            "already_acked":[1],"unknown":[]}
        with self.assertRaisesRegex(RuntimeError,"MCP_ACK_UNCONFIRMED"):
            b.process_pending(lambda _:True,ack_batch_size=2)
        b.ack.assert_called_once_with([1,2])

    def test_ack_batch_size_rejects_invalid_numbers_without_network(self):
        b=self.bridge([item(1)])
        for n in (0,21,True,-1,1.2,"4"):
            with self.subTest(n=n):
                with self.assertRaisesRegex(ValueError,"INVALID_ACK_BATCH_SIZE"):
                    b.process_pending(lambda _:True,ack_batch_size=n)
        b.receive.assert_not_called()

    def test_opt_in_batch_ack_accepts_partial_replay_receipt(self):
        b=self.bridge([item(1),item(2)])
        b.ack.side_effect=None
        b.ack.return_value={"ok":True,"acked":1,
                            "already_acked":[1],"unknown":[]}
        result=b.process_pending(lambda _:True,ack_batch_size=2)
        self.assertEqual(result["acknowledged"],2)
        b.ack.assert_called_once_with([1,2])

    def test_opt_in_batch_ack_rejects_unknown_ids(self):
        b=self.bridge([item(1),item(2)])
        b.ack.side_effect=None
        b.ack.return_value={"ok":True,"acked":1,
                            "already_acked":[],"unknown":[2]}
        with self.assertRaisesRegex(RuntimeError,"MCP_ACK_UNCONFIRMED"):
            b.process_pending(lambda _:True,ack_batch_size=2)
        b.ack.assert_called_once_with([1,2])

    def test_opt_in_batch_ack_timeout_preserves_uncertainty(self):
        b=self.bridge([item(1),item(2),item(3)])
        b.ack.side_effect=TimeoutError("ACK_BATCH_RESPONSE_LOST")
        observed=[]
        with self.assertRaisesRegex(TimeoutError,"ACK_BATCH_RESPONSE_LOST"):
            b.process_pending(lambda x: observed.append(x["id"]) or True,
                              ack_batch_size=2)
        self.assertEqual(observed,[1,2])
        b.ack.assert_called_once_with([1,2])

    def test_opt_in_batch_ack_releases_lock_after_error(self):
        b=self.bridge([item(1),item(2)])
        b.ack.side_effect=TimeoutError("FIRST_ACK_LOST")
        with self.assertRaisesRegex(TimeoutError,"FIRST_ACK_LOST"):
            b.process_pending(lambda _:True,ack_batch_size=2)
        b.ack.side_effect=None
        b.ack.return_value={"ok":True,"acked":2,"already_acked":[],"unknown":[]}
        self.assertEqual(b.process_pending(lambda _:True,ack_batch_size=2)["acknowledged"],2)


    def test_handler_cannot_retarget_ack_by_mutating_id(self):
        b=self.bridge([item(1),item(2)])
        def handler(message):
            if message["id"]==1:
                message["id"]=2
            return True
        result=b.process_pending(handler,ack_batch_size=2)
        self.assertEqual(result["acknowledged"],2)
        b.ack.assert_called_once_with([1,2])

    def test_handler_cannot_retarget_deferred_id(self):
        b=self.bridge([item(7)])
        def handler(message):
            message["id"]=999
            return False
        result=b.process_pending(handler)
        self.assertEqual(result["deferred_id"],7)
        self.assertEqual(result["acknowledged"],0)
        b.ack.assert_not_called()

    def test_standalone_without_hub_cannot_consume(self):
        b=MCPBridge(token="")
        with self.assertRaisesRegex(RuntimeError,"MCP_NOT_CONNECTED_STANDALONE"):
            b.process_pending(lambda _:True)


    def test_exact_acked_ids_must_cover_all_processed_messages(self):
        bad_confirmations=(
            [41,41],
            [41,99],
            [41],
            ["41",42],
            "41,42",
            None,
        )
        for confirmed in bad_confirmations:
            with self.subTest(confirmed=confirmed):
                b=self.bridge([item(41),item(42)])
                b.ack.side_effect=None
                b.ack.return_value={
                    "ok":True,"acked":2,"already_acked":[],"unknown":[],
                    "acked_ids":confirmed}
                # A field explicitly set to null is malformed, unlike
                # a genuinely absent field from a backward-compatible hub.
                with self.assertRaisesRegex(RuntimeError,"MCP_ACK_UNCONFIRMED"):
                    b.process_pending(lambda _:True,ack_batch_size=2)
                b.ack.assert_called_once_with([41,42])
        old_hub=self.bridge([item(41),item(42)])
        old_hub.ack.side_effect=None
        old_hub.ack.return_value={
            "ok":True,"acked":2,"already_acked":[],"unknown":[]}
        self.assertEqual(
            old_hub.process_pending(lambda _:True,ack_batch_size=2)[
                "acknowledged"],2)

        b=self.bridge([item(41),item(42)])
        b.ack.side_effect=None
        b.ack.return_value={
            "ok":True,"acked":2,"acked_ids":[42,41],
            "already_acked":[],"unknown":[]}
        self.assertEqual(
            b.process_pending(lambda _:True,ack_batch_size=2)["acknowledged"],2)

    def test_mismatched_inbox_or_ack_recipient_fails_closed(self):
        import module_bridge

        module_id=module_bridge.CANONICAL_MODULE_ID
        peer="esp" if module_id!="esp" else "android"
        candidate=self.bridge([item(77)])
        handler=Mock(return_value=True)
        candidate.receive.return_value={
            "requires_ack":True,"recipient":peer,
            "messages":[item(77)],"pending":1}
        with self.assertRaisesRegex(ValueError,"MCP_INBOX_RECIPIENT_MISMATCH"):
            candidate.process_pending(handler)
        handler.assert_not_called()
        candidate.ack.assert_not_called()

        candidate=self.bridge([item(78)])
        candidate.receive.return_value={
            "requires_ack":True,"recipient":module_id,
            "messages":[item(78)],"pending":1}
        candidate.ack.side_effect=None
        candidate.ack.return_value={
            "ok":True,"recipient":peer,"acked":1,
            "acked_ids":[78],"already_acked":[],"unknown":[]}
        with self.assertRaisesRegex(RuntimeError,"MCP_ACK_UNCONFIRMED"):
            candidate.process_pending(lambda _:True)
        candidate.ack.assert_called_once_with([78])

        candidate.ack.reset_mock()
        candidate.ack.return_value={
            "ok":True,"recipient":module_id,"acked":1,
            "acked_ids":[78],"already_acked":[],"unknown":[]}
        report=candidate.process_pending(lambda _:True)
        self.assertEqual(report["acknowledged"],1)


def test_consumer_lease_key_is_canonical_across_installations(tmp_path):
    import json
    from pathlib import Path
    from unittest.mock import patch
    import module_bridge

    declared=json.loads(
        (Path(__file__).resolve().parents[1]/"module.mdl").read_text(
            encoding="utf-8"))["id"]
    assert module_bridge.CANONICAL_MODULE_ID==declared
    initial=module_bridge._consumer_lease_path()
    assert initial.endswith(".lock")
    assert "mcp-module-inbox-" in initial
    # A second checkout of the same package may resolve __file__ differently,
    # but must still contend for the same inbox-consumer OS lock.
    with patch.object(module_bridge,"__file__",
                      str(tmp_path/"copy-two"/"module_bridge.py")):
        assert module_bridge._consumer_lease_path()==initial
    # Independent canonical module identities must not block each other.
    with patch.object(module_bridge,"CANONICAL_MODULE_ID",declared+"-other"):
        assert module_bridge._consumer_lease_path()!=initial
