import unittest
from easychange.remote.optical_protocol import encode_result_chunks
from mcp_adapter.optical_reader import OpticalAssembler

class OpticalReaderTests(unittest.TestCase):
    def test_ec2_round_trip_with_integrity_check(self):
        expected={"sequence":"example-1","ok":True,"code":"OK","data":{"text":"x"*500}}
        chunks=encode_result_chunks(expected)
        session=OpticalAssembler()
        mid=session.ingest(chunks[:1])
        self.assertIn("complete",mid)
        result=session.ingest(chunks[1:])
        self.assertTrue(result["complete"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["payload"]["data"]["text"],"x"*500)
    def test_foreign_sequence_does_not_mix(self):
        a=OpticalAssembler()
        one=encode_result_chunks({"sequence":"one","ok":True})
        other=encode_result_chunks({"sequence":"other","ok":True})
        out=a.ingest(one+other)
        self.assertTrue(out["verified"])
        self.assertEqual(out["sequence"],"one")
    def test_malformed_or_oversized_qr_does_not_claim_session(self):
        import json
        from unittest.mock import patch
        from easychange.core.result import Result
        from mcp_adapter import optical_reader

        packets=encode_result_chunks(
            Result(True,"read",{"text":"valid"*200},sequence="QOPTSAFE1"))
        bad=json.loads(packets[0])
        bad["n"]=10**9
        assembly=OpticalAssembler()
        with patch.object(optical_reader,"MAX_EC2_PACKET_TEXT_CHARS",16):
            state=assembly.ingest([packets[0]])
        self.assertIsNone(state["sequence"])
        self.assertEqual(assembly.stored_chars,0)

        state=assembly.ingest([json.dumps(bad)])
        self.assertIsNone(state["sequence"])
        self.assertEqual(assembly.chunks,{})
        self.assertTrue(assembly.ingest(packets)["complete"])

    def test_qr_assembly_byte_budget_keeps_last_good_frame_available(self):
        from unittest.mock import patch
        from mcp_adapter import optical_reader
        from easychange.core.result import Result

        packets=encode_result_chunks(
            Result(True,"read",{"text":"v"*600},sequence="QOPTCAP1"))
        session=OpticalAssembler()
        with patch.object(optical_reader,"MAX_EC2_ASSEMBLY_TEXT_CHARS",8):
            blocked=session.ingest(packets)
        self.assertFalse(blocked["complete"])
        self.assertIsNone(blocked["sequence"])
        self.assertEqual(session.stored_chars,0)
        done=session.ingest(packets)
        self.assertTrue(done["complete"])
        self.assertLessEqual(session.stored_chars,sum(len(packet) for packet in packets))

    def test_equivalent_duplicate_qr_only_replaces_stored_bytes(self):
        from easychange.core.result import Result
        packets=encode_result_chunks(
            Result(True,"read",{"text":"z"*1000},sequence="QOPTDUP1"))
        session=OpticalAssembler()
        session.ingest(packets[:1])
        replacement=packets[0]+"  "
        session.ingest([replacement])
        self.assertEqual(session.stored_chars,len(replacement))
        self.assertEqual(len(session.chunks),1)
        assert session.ingest(packets[1:])["complete"]


if __name__=="__main__":
    unittest.main()
