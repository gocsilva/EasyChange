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
    def test_provisional_durable_header_hash_does_not_alias_wire_hash(self):
        import hashlib
        import json
        from easychange.remote.optical_protocol import encode_durable_result_header

        child={"text":"one","bytes":123}
        original={"sequence":"QHDRDIFF1","ok":True,"command":"structured",
                  "data":{"results":[child],"primary":dict(child)}}
        packets=encode_result_chunks(original)
        serialized=json.dumps(original,ensure_ascii=False,
                              separators=(",",":")).encode("utf-8")
        durable_digest=hashlib.sha256(serialized).hexdigest()
        wire_digest=json.loads(packets[0])["h"]
        # A compact $ec_ref produces distinct verified wire bytes.
        self.assertNotEqual(durable_digest[:16],wire_digest)
        provisional=encode_durable_result_header({
            "sequence":"QHDRDIFF1","finished":True,"success":True,
            "sha256":durable_digest,"payload_length":len(serialized)})
        collector=OpticalAssembler()
        self.assertTrue(collector.ingest(packets)["complete"])
        # The provisional header is useful metadata, not the wire digest.
        self.assertTrue(collector.ingest([provisional])["complete"])
        self.assertEqual(collector.ingest([])["payload"]["data"],original["data"])

        self.assertEqual(collector.header["h"],durable_digest)
        self.assertEqual(collector.header["d"],1)
        final=json.loads(provisional)
        final.pop("d",None)
        final["n"]=len(packets)
        # A mismatched final header must not poison the verified transfer
        # or overwrite a legitimate provisional durable receipt.
        self.assertTrue(collector.ingest([json.dumps(final)])["verified"])
        self.assertEqual(collector.header["h"],durable_digest)
        final["h"]=wire_digest
        self.assertTrue(collector.ingest([json.dumps(final)])["verified"])
        self.assertEqual(collector.header["h"],wire_digest)
        # A delayed provisional QR must not downgrade a final EC2H receipt.
        collector.ingest([provisional])
        self.assertEqual(collector.header["h"],wire_digest)

    def test_durable_header_shown_before_chunks_waits_for_matching_sequence(self):
        import hashlib
        import json
        from easychange.remote.optical_protocol import encode_durable_result_header

        original={"sequence":"QHDRFIRST1","ok":True,"data":{"text":"ready"}}
        wire=encode_result_chunks(original)
        original_bytes=json.dumps(
            original,ensure_ascii=False,separators=(",",":")).encode("utf-8")
        durable=encode_durable_result_header({
            "sequence":"QHDRFIRST1","finished":True,"success":True,
            "sha256":hashlib.sha256(original_bytes).hexdigest(),
            "payload_length":len(original_bytes),
        })
        receiver=OpticalAssembler()
        awaiting=receiver.ingest([durable])
        self.assertFalse(awaiting["complete"])
        self.assertIsNone(awaiting["sequence"])
        self.assertEqual(receiver.header,{})
        self.assertEqual(receiver.pending_provisional_header["d"],1)

        other=encode_result_chunks({"sequence":"FOREIGN","ok":True})
        other_receiver=OpticalAssembler()
        other_receiver.ingest([durable])
        other_receiver.ingest(other[:1])
        self.assertEqual(other_receiver.header,{})
        self.assertEqual(other_receiver.pending_provisional_header,{})

        completed=receiver.ingest(wire)
        self.assertTrue(completed["verified"])
        self.assertEqual(receiver.header["d"],1)
        self.assertEqual(receiver.pending_provisional_header,{})

        # The verified final EC2H may replace the provisional header; a
        # late repeat of the provisional QR must not downgrade it again.
        final=json.loads(durable)
        final.pop("d")
        final["n"]=len(wire)
        final["h"]=json.loads(wire[0])["h"]
        receiver.ingest([json.dumps(final)])
        self.assertEqual(receiver.header["h"],final["h"])
        receiver.ingest([durable])
        self.assertEqual(receiver.header["h"],final["h"])

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


    def test_corrupt_first_qr_is_skipped_allowing_valid_retransmission(self):
        import json
        import hashlib
        from easychange.core.result import Result

        expected="".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(90))
        packets=encode_result_chunks(
            Result(True,"read",{"text":expected},sequence="QRECOVER01"))
        assert len(packets) >= 2
        forged=json.loads(packets[0])
        forged["d"]="!"+forged["d"][1:]
        # Preserve the old CRC: this packet is corrupted in transit.
        collector=OpticalAssembler()
        initial=collector.ingest([json.dumps(forged)])
        self.assertIsNone(initial["sequence"])
        self.assertEqual(initial["received"],0)
        receipt=collector.ingest(packets)
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["payload"]["data"]["text"],expected)

    def test_corrupt_later_qr_does_not_poison_existing_assembly(self):
        import json
        import hashlib
        from easychange.core.result import Result

        expected="".join(hashlib.sha256(("b"+str(i)).encode()).hexdigest() for i in range(90))
        packets=encode_result_chunks(
            Result(True,"read",{"text":expected},sequence="QRECOVER02"))
        assert len(packets)>=2
        bad=json.loads(packets[1])
        bad["d"]="!"+bad["d"][1:]
        collector=OpticalAssembler()
        first=collector.ingest([packets[0],json.dumps(bad)])
        self.assertFalse(first["complete"])
        self.assertEqual(first["received"],1)
        full=collector.ingest(packets[1:])
        self.assertTrue(full["complete"])
        self.assertEqual(full["payload"]["data"]["text"],expected)

    def test_aggregate_token_budget_rejects_qr_without_losing_next_valid_frame(self):
        from unittest.mock import patch
        from easychange.core.result import Result
        from mcp_adapter import optical_reader
        packets=encode_result_chunks(
            Result(True,"read",{"text":"payload"},sequence="QTOKEN01"))
        receiver=OpticalAssembler()
        with patch.object(optical_reader,"MAX_EC2_TOKEN_CHARS",1):
            pending=receiver.ingest(packets)
        self.assertFalse(pending["complete"])
        self.assertIsNone(pending["sequence"])
        self.assertEqual(receiver.stored_token_chars,0)
        self.assertTrue(receiver.ingest(packets)["complete"])
        self.assertEqual(receiver.stored_token_chars,
                         sum(len(__import__("json").loads(raw)["d"]) for raw in receiver.chunks.values()))
        # Replayed duplicate frames cannot consume the byte budget again.
        saved=receiver.stored_token_chars
        receiver.ingest(packets)
        self.assertEqual(receiver.stored_token_chars,saved)

    def test_repeated_complete_carousel_decodes_only_once(self):
        from unittest.mock import patch
        from easychange.core.result import Result
        from mcp_adapter import optical_reader
        packets=encode_result_chunks(
            Result(True,"read",{"value":"hello"*300},sequence="QCACHE01"))
        assembly=OpticalAssembler()
        with patch.object(optical_reader,"decode_result_chunks",
                          wraps=optical_reader.decode_result_chunks) as decoder:
            first=assembly.ingest(packets)
            self.assertTrue(first["complete"])
            for _ in range(20):
                repeated=assembly.ingest(packets)
                self.assertTrue(repeated["complete"])
                self.assertIs(repeated["payload"],first["payload"])
            decoder.assert_called_once_with(list(assembly.chunks.values()))

    def test_mismatched_packet_manifest_cannot_poison_later_retransmission(self):
        import json
        from easychange.core.result import Result
        import hashlib
        sample="".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(64))
        packets=encode_result_chunks(
            Result(True,"read",{"text":sample},sequence="QIDENT01"))
        self.assertGreaterEqual(len(packets),2)
        assembly=OpticalAssembler()
        assembly.ingest(packets[:1])
        corrupted=json.loads(packets[1])
        corrupted["h"]="0"*16 if corrupted["h"]!="0"*16 else "1"*16
        state=assembly.ingest([json.dumps(corrupted)])
        self.assertFalse(state["complete"])
        self.assertEqual(state["received"],1)
        result=assembly.ingest(packets[1:])
        self.assertTrue(result["complete"])
        self.assertEqual(result["payload"]["data"]["text"],sample)

    def test_mismatched_completion_header_cannot_poison_valid_qr_replay(self):
        import json
        import hashlib
        from easychange.core.result import Result

        content="".join(hashlib.sha256(str(i).encode()).hexdigest()
                        for i in range(90))
        packets=encode_result_chunks(
            Result(True,"read",{"text":content},sequence="QHEADERSAFE"))
        self.assertGreaterEqual(len(packets),2)
        first=json.loads(packets[0])
        session=OpticalAssembler()
        session.ingest(packets[:1])
        foreign={"p":"EC2H","s":"QHEADERSAFE","f":True,
                 "h":"0"*64 if first["h"]!="0"*16 else "1"*64}
        pending=session.ingest([json.dumps(foreign)])
        self.assertEqual(pending["header"],{})
        self.assertEqual(session.header,{})
        self.assertTrue(session.ingest(packets[1:])["verified"])
        valid={"p":"EC2H","s":"QHEADERSAFE","f":True,
               "h":first["h"]+"0"*48}
        complete=session.ingest([json.dumps(valid)])
        self.assertTrue(complete["verified"])
        self.assertEqual(session.header["h"],valid["h"])


    def test_recalculated_crc_cannot_make_invalid_base64url_poison_session(self):
        import json
        import binascii
        from easychange.core.result import Result

        packets=encode_result_chunks(
            Result(True,"read",{"value":"hello"},sequence="QURLSAFE1"))
        forged=json.loads(packets[0])
        forged["d"]="!"+forged["d"]
        forged["x"]=f"{binascii.crc32(forged['d'].encode('ascii')) & 0xffffffff:08x}"
        assembly=OpticalAssembler()
        state=assembly.ingest([json.dumps(forged)])
        self.assertFalse(state["complete"])
        self.assertIsNone(state["sequence"])
        self.assertEqual(assembly.chunks,{})
        self.assertTrue(assembly.ingest(packets)["complete"])

    def test_untrusted_header_requires_real_boolean_completion(self):
        import json
        from easychange.core.result import Result

        packets=encode_result_chunks(
            Result(True,"read",{"value":"ok"},sequence="QHDRBOOL1"))
        first=json.loads(packets[0])
        assembler=OpticalAssembler()
        assembler.ingest(packets)
        header={"p":"EC2H","s":"QHDRBOOL1","f":"false",
                "h":first["h"]}
        assembler.ingest([json.dumps(header)])
        self.assertEqual(assembler.header,{})
        header["f"]=True
        assembler.ingest([json.dumps(header)])
        self.assertTrue(assembler.header["f"])


if __name__=="__main__":
    unittest.main()
