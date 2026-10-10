import unittest
from easychange.remote.optical_protocol import encode_result_chunks
from mcp_adapter.optical_reader import OpticalAssembler

class OpticalReaderTests(unittest.TestCase):
    def test_no_frame_does_not_initialize_or_call_qr_detector(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from mcp_adapter import optical_reader as reader

        qr=Mock()
        cv2_stub=SimpleNamespace(error=RuntimeError,QRCodeDetector=qr)
        with patch.dict(sys.modules,{"cv2":cv2_stub}):
            for frame in (None,object(),
                          SimpleNamespace(shape=(0,320,3)),
                          SimpleNamespace(shape=(240,0,3)),
                          SimpleNamespace(shape=("bad",320,3))):
                with self.subTest(frame=frame):
                    self.assertEqual(reader.decode_hdmi_qr(frame),[])
        qr.assert_not_called()

    def test_absurd_dimensions_and_invalid_channels_skip_qr_decoder(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from mcp_adapter import optical_reader as reader

        qr=Mock()
        cv2_stub=SimpleNamespace(error=RuntimeError,QRCodeDetector=qr)
        invalid=(
            (100_000,100_000,3),
            (7680,9000,3),
            (240,320,2),
            (240,320,"3"),
            (240,320,0),
        )
        with patch.dict(sys.modules,{"cv2":cv2_stub}):
            for dims in invalid:
                with self.subTest(shape=dims):
                    self.assertEqual(
                        reader.decode_hdmi_qr(SimpleNamespace(shape=dims)),[])
        qr.assert_not_called()

    def test_multi_qr_detector_output_has_fixed_per_frame_iteration_budget(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from mcp_adapter import optical_reader as reader

        visited=[]
        def endless_blanks():
            number=0
            while True:
                visited.append(number)
                number+=1
                yield ""

        detector=Mock()
        detector.detectAndDecodeMulti.return_value=(
            True,endless_blanks(),None,None)
        detector.detectAndDecode.return_value=("EC2_SINGLE_RECOVERED",None,None)
        cv2_stub=SimpleNamespace(error=RuntimeError,QRCodeDetector=lambda:detector)
        frame=SimpleNamespace(shape=(360,640,3))
        with patch.dict(sys.modules,{"cv2":cv2_stub}), \
             patch.object(reader._qr_detectors,"detector",None,create=True):
            self.assertEqual(reader.decode_hdmi_qr(frame),
                             ["EC2_SINGLE_RECOVERED"])
        self.assertEqual(len(visited),32)
        detector.detectAndDecode.assert_called_once_with(frame)

    def test_string_multi_qr_result_is_not_treated_as_individual_characters(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from mcp_adapter import optical_reader as reader

        detector=Mock()
        detector.detectAndDecodeMulti.return_value=(True,"EC2_INVALID",None,None)
        detector.detectAndDecode.return_value=("EC2_SINGLE_VALID",None,None)
        cv2_stub=SimpleNamespace(error=RuntimeError,QRCodeDetector=lambda:detector)
        with patch.dict(sys.modules,{"cv2":cv2_stub}), \
             patch.object(reader._qr_detectors,"detector",None,create=True):
            result=reader.decode_hdmi_qr(SimpleNamespace(shape=(400,600,3)))
        self.assertEqual(result,["EC2_SINGLE_VALID"])


    def test_single_only_opencv_detector_still_reads_ec2_qr(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        from mcp_adapter import optical_reader as reader

        class SingleOnly:
            def __init__(self):
                self.calls=0
            def detectAndDecode(self,frame):
                self.calls+=1
                return "EC2_SINGLE_QR",None,None

        detector=SingleOnly()
        cv2_stub=SimpleNamespace(
            error=RuntimeError,QRCodeDetector=lambda:detector)
        image=SimpleNamespace(shape=(480,640,3))
        with patch.dict(sys.modules,{"cv2":cv2_stub}), \
             patch.object(reader._qr_detectors,"detector",None,create=True):
            self.assertEqual(reader.decode_hdmi_qr(image),["EC2_SINGLE_QR"])
            self.assertEqual(reader.decode_hdmi_qr(image),["EC2_SINGLE_QR"])
        self.assertEqual(detector.calls,2)


    def test_multi_qr_signature_type_error_recovers_same_frame_via_single(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from mcp_adapter import optical_reader as reader

        detector=Mock()
        detector.detectAndDecodeMulti.side_effect=[
            TypeError("UNEXPECTED_IMAGE_BUFFER"),
            (False,[],None,None),
        ]
        detector.detectAndDecode.return_value=("EC2_RECOVERED",None,None)
        cv2_stub=SimpleNamespace(error=RuntimeError,
                                 QRCodeDetector=lambda:detector)
        image=SimpleNamespace(shape=(480,640,3))
        with patch.dict(sys.modules,{"cv2":cv2_stub}), \
             patch.object(reader._qr_detectors,"detector",None,create=True):
            self.assertEqual(reader.decode_hdmi_qr(image),["EC2_RECOVERED"])
            self.assertEqual(reader.decode_hdmi_qr(image),["EC2_RECOVERED"])
        self.assertEqual(detector.detectAndDecodeMulti.call_count,2)
        self.assertEqual(detector.detectAndDecode.call_count,2)

    def test_transient_qr_decoder_exception_allows_next_frame(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from mcp_adapter import optical_reader as reader

        class InvalidImage(Exception):
            pass
        detector=Mock()
        detector.detectAndDecodeMulti.side_effect=[
            InvalidImage("CAPTURE_INTERRUPTED"),
            (False, [], None, None),
        ]
        detector.detectAndDecode.return_value=("EC2_VALID",None,None)
        cv2_stub=SimpleNamespace(error=InvalidImage,QRCodeDetector=lambda:detector)
        frame=SimpleNamespace(shape=(480,640,3))
        with patch.dict(sys.modules,{"cv2":cv2_stub}), \
             patch.object(reader._qr_detectors,"detector",None,create=True):
            self.assertEqual(reader.decode_hdmi_qr(frame),[])
            self.assertEqual(reader.decode_hdmi_qr(frame),["EC2_VALID"])
        self.assertEqual(detector.detectAndDecodeMulti.call_count,2)

    def test_failed_detector_construction_does_not_poison_next_frame(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from mcp_adapter import optical_reader as reader

        qr=Mock()
        qr.detectAndDecodeMulti.return_value=(False,[],None,None)
        qr.detectAndDecode.return_value=("EC2_READY",None,None)
        maker=Mock(side_effect=[RuntimeError("OPENCV_DEVICE_RESET"),qr])
        cv2_stub=SimpleNamespace(error=RuntimeError,QRCodeDetector=maker)
        frame=SimpleNamespace(shape=(1080,1920,3))
        with patch.dict(sys.modules,{"cv2":cv2_stub}), \
             patch.object(reader._qr_detectors,"detector",None,create=True):
            self.assertEqual(reader.decode_hdmi_qr(frame),[])
            self.assertEqual(reader.decode_hdmi_qr(frame),["EC2_READY"])
        self.assertEqual(maker.call_count,2)

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

    def test_stale_qr_session_can_accept_a_new_transfer_without_mixing(self):
        import hashlib
        from mcp_adapter import optical_reader

        sample="".join(hashlib.sha256(str(i).encode()).hexdigest()
                       for i in range(80))
        first=encode_result_chunks({"sequence":"OLD-QR","ok":True,
                                    "data":{"text":sample}})
        later=encode_result_chunks({"sequence":"NEW-QR","ok":True,
                                    "data":{"text":"new result"}})
        self.assertGreater(len(first),1)
        session=OpticalAssembler()
        session.ingest(first[:1])
        blocked=session.ingest(later)
        self.assertFalse(blocked["complete"])
        self.assertEqual(blocked["sequence"],"OLD-QR")
        session.updated_at-=optical_reader.MAX_EC2_ASSEMBLY_IDLE_SECONDS + 1
        recovered=session.ingest(later)
        self.assertTrue(recovered["verified"])
        self.assertEqual(recovered["sequence"],"NEW-QR")
        self.assertEqual(recovered["payload"]["data"]["text"],"new result")
        self.assertEqual(session.stored_token_chars,
                         sum(len(__import__("json").loads(raw)["d"])
                             for raw in session.chunks.values()))

    def test_verified_ec2_result_hands_off_immediately_to_new_transfer(self):
        import hashlib
        import json

        first=encode_result_chunks({
            "sequence":"EC2-OLD-DONE","ok":True,"data":{"text":"first"}})
        next_text="".join(hashlib.sha256(str(i).encode()).hexdigest()
                          for i in range(90))
        next_packets=encode_result_chunks({
            "sequence":"EC2-NEXT-DONE","ok":True,"data":{"text":next_text}})
        self.assertGreater(len(next_packets),1)
        assembler=OpticalAssembler()
        old=assembler.ingest(first)
        self.assertTrue(old["verified"])
        self.assertEqual(old["sequence"],"EC2-OLD-DONE")
        # A late packet from another carousel cannot evict the verified
        # result before the beginning (index zero) arrives.
        still_old=assembler.ingest(next_packets[1:])
        self.assertTrue(still_old["verified"])
        self.assertEqual(still_old["sequence"],"EC2-OLD-DONE")
        first_new=assembler.ingest(next_packets[:1])
        self.assertFalse(first_new["complete"])
        self.assertEqual(first_new["sequence"],"EC2-NEXT-DONE")
        result=assembler.ingest(next_packets[1:])
        self.assertTrue(result["verified"])
        self.assertEqual(result["sequence"],"EC2-NEXT-DONE")
        self.assertEqual(result["payload"]["data"]["text"],next_text)
        self.assertEqual(assembler.ingest([])["sequence"],"EC2-NEXT-DONE")

    def test_bad_crc_start_packet_cannot_evict_verified_transfer(self):
        import json

        first=encode_result_chunks({
            "sequence":"EC2-SAFE-DONE","ok":True,"data":{"text":"verified"}})
        next_packets=encode_result_chunks({
            "sequence":"EC2-CORRUPT-NEXT","ok":True,"data":{"text":"next"}})
        assembler=OpticalAssembler()
        assembler.ingest(first)
        invalid=json.loads(next_packets[0])
        invalid["d"]="!"+invalid["d"][1:]
        held=assembler.ingest([json.dumps(invalid)])
        self.assertTrue(held["verified"])
        self.assertEqual(held["sequence"],"EC2-SAFE-DONE")
        verified=assembler.ingest(next_packets)
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["sequence"],"EC2-CORRUPT-NEXT")


    def test_unpaired_provisional_header_expires_only_on_new_frames(self):
        from easychange.remote.optical_protocol import encode_durable_result_header
        from mcp_adapter import optical_reader

        provisional=encode_durable_result_header({
            "sequence":"QEXPIREDHEAD","finished":True,"success":True,
            "sha256":"a"*64,"payload_length":17,
        })
        session=OpticalAssembler()
        session.ingest([provisional])
        self.assertEqual(session.pending_provisional_header["s"],"QEXPIREDHEAD")
        session.updated_at-=optical_reader.MAX_EC2_ASSEMBLY_IDLE_SECONDS+1
        session.ingest([])
        self.assertTrue(session.pending_provisional_header)
        # A later frame starts a new observation window. A stale isolated
        # header must not survive indefinitely waiting for its EC2 chunks.
        stale=session.ingest(['{}'])
        self.assertIsNone(stale["sequence"])
        self.assertEqual(session.pending_provisional_header,{})
        self.assertEqual(session.header,{})
        session.ingest([provisional])
        self.assertEqual(session.pending_provisional_header["s"],"QEXPIREDHEAD")

    def test_provisional_header_updates_idle_deadline_on_acceptance(self):
        from easychange.remote.optical_protocol import encode_durable_result_header
        provisional=encode_durable_result_header({
            "sequence":"QHEARTBEATHEAD","finished":True,"success":True,
            "sha256":"b"*64,"payload_length":23,
        })
        session=OpticalAssembler()
        session.updated_at=0.0
        session.ingest([provisional])
        self.assertGreater(session.updated_at,0.0)
        self.assertEqual(session.pending_provisional_header["s"],"QHEARTBEATHEAD")

    def test_idle_expiry_does_not_discard_last_result_on_empty_poll(self):
        from mcp_adapter import optical_reader

        session=OpticalAssembler()
        frames=encode_result_chunks({"sequence":"DONE-QR","ok":True})
        done=session.ingest(frames)
        self.assertTrue(done["verified"])
        session.updated_at-=optical_reader.MAX_EC2_ASSEMBLY_IDLE_SECONDS + 1
        self.assertIs(session.ingest([])["payload"],done["payload"])
        self.assertEqual(session.sequence,"DONE-QR")


    def test_foreign_sequence_does_not_mix(self):
        a=OpticalAssembler()
        one=encode_result_chunks({"sequence":"one","ok":True})
        other=encode_result_chunks({"sequence":"other","ok":True})
        out=a.ingest(one+other)
        self.assertTrue(out["verified"])
        self.assertEqual(out["sequence"],"one")
    def test_duplicate_json_fields_and_nonfinite_numbers_cannot_claim_qr_slots(self):
        import json
        from mcp_adapter.optical_reader import OpticalAssembler

        packets=encode_result_chunks({
            "sequence":"QSTRICTQR2026","ok":True,"data":{"text":"valid"}
        })
        self.assertTrue(packets)
        packet=packets[0]
        # The normal wire JSON already has an s field, so this prefixed s
        # creates a duplicate key even if both values happen to match.
        invalid=(
            '{"s":"FOREIGN",' + packet[1:],
            '{"noise":NaN,' + packet[1:],
            '{"noise":Infinity,' + packet[1:],
            '{"p":"EC2H","p":"EC2",' + packet[1:],
        )
        assembler=OpticalAssembler()
        for raw in invalid:
            with self.subTest(raw=raw[:70]):
                result=assembler.ingest([raw])
                self.assertFalse(result["complete"])
                self.assertIsNone(result["sequence"])
                self.assertEqual(assembler.chunks,{})
                self.assertEqual(assembler.stored_token_chars,0)
                self.assertEqual(assembler.header,{})
                self.assertEqual(assembler.pending_provisional_header,{})
        verified=assembler.ingest(packets)
        self.assertTrue(verified["complete"])
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["sequence"],"QSTRICTQR2026")
        self.assertEqual(verified["payload"]["data"]["text"],"valid")

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

    def test_complete_bad_hash_carousel_releases_slot_for_valid_retransmission(self):
        import hashlib
        import json
        from easychange.core.result import Result

        data="".join(hashlib.sha256(str(number).encode()).hexdigest()
                     for number in range(100))
        valid=encode_result_chunks(
            Result(True,"read",{"text":data},sequence="QRETRANSMIT"),
            chunk_chars=160)
        self.assertGreater(len(valid),2)
        first=json.loads(valid[0])
        wrong_digest=("0" if first["h"][0]!="0" else "1")+first["h"][1:]
        bad=[]
        for raw in valid:
            packet=json.loads(raw)
            packet["h"]=wrong_digest
            bad.append(json.dumps(packet))

        receiver=OpticalAssembler()
        with self.assertRaisesRegex(ValueError,"optical sha256 mismatch"):
            receiver.ingest(bad)
        self.assertEqual(receiver.sequence,"")
        self.assertEqual(receiver.chunks,{})
        self.assertEqual(receiver.total,0)
        self.assertEqual(receiver.stored_chars,0)
        self.assertEqual(receiver.stored_token_chars,0)
        self.assertIsNone(receiver.packet_identity)
        self.assertIsNone(receiver.verified_payload)
        replay=receiver.ingest(valid)
        self.assertTrue(replay["complete"])
        self.assertTrue(replay["verified"])
        self.assertEqual(replay["payload"]["data"]["text"],data)

    def test_full_qr_carousel_decodes_from_existing_view_without_list_copy(self):
        from unittest.mock import patch
        import hashlib
        import mcp_adapter.optical_reader as reader

        content="".join(hashlib.sha256(str(i).encode()).hexdigest()
                        for i in range(100))
        packets=encode_result_chunks(
            {"sequence":"QVIEW2026","ok":True,"data":{"text":content}},
            chunk_chars=120)
        self.assertGreater(len(packets),3)
        assembler=OpticalAssembler()
        original=reader.decode_result_chunks
        received=[]
        def capture(chunk_collection):
            received.append(chunk_collection)
            self.assertIsInstance(chunk_collection,type({}.values()))
            self.assertNotIsInstance(chunk_collection,list)
            return original(chunk_collection)
        with patch.object(reader,"decode_result_chunks",side_effect=capture):
            result=assembler.ingest(packets)
        self.assertTrue(result["verified"])
        self.assertEqual(result["payload"]["data"]["text"],content)
        self.assertEqual(len(received),1)

    def test_exact_repeated_qr_avoids_rechecking_accepted_crc(self):
        import hashlib
        from unittest.mock import patch
        from mcp_adapter import optical_reader as reader
        from easychange.core.result import Result

        payload="".join(hashlib.sha256(str(i).encode()).hexdigest()
                        for i in range(75))
        packets=encode_result_chunks(
            Result(True,"read",{"text":payload},sequence="QREPLAYFAST"),
            chunk_chars=150)
        session=OpticalAssembler()
        initial=session.ingest(packets)
        self.assertTrue(initial["verified"])
        previous=session.stored_chars
        previous_token=session.stored_token_chars

        # The exact same bytes were already verified on admission. A new
        # packet, including a mutated duplicate, still requires CRC checking.
        with patch.object(reader.binascii,"crc32",
                          side_effect=AssertionError("REDUNDANT_CRC")) as crc:
            repeated=session.ingest(list(reversed(packets)))
        crc.assert_not_called()
        self.assertTrue(repeated["verified"])
        self.assertIs(repeated["payload"],initial["payload"])
        self.assertEqual(session.stored_chars,previous)
        self.assertEqual(session.stored_token_chars,previous_token)

        import json
        changed=json.loads(packets[0])
        changed["d"]=("A" if changed["d"][0]!="A" else "B")+changed["d"][1:]
        with patch.object(reader.binascii,"crc32", wraps=reader.binascii.crc32) as crc:
            session.ingest([json.dumps(changed)])
        crc.assert_called_once()
        self.assertTrue(session.ingest([])["verified"])

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
            decoder.assert_called_once()
            received=decoder.call_args.args[0]
            # The decoder receives a live dict_values view, avoiding one
            # unnecessary list allocation for large EC2 carousels.
            self.assertIsInstance(received,type(assembly.chunks.values()))
            self.assertEqual(list(received),list(assembly.chunks.values()))

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

    def test_invalid_transport_marker_cannot_poison_qr_slot(self):
        import json

        original={"sequence":"QLITERALHDMI1","ok":True,
                  "data":{"results":[{"value":"actual"}],
                          "primary":{"$ec_ref":"results[0]"},
                          "transport_compacted":True}}
        packets=encode_result_chunks(original)
        self.assertEqual(json.loads(packets[0])["t"],0)
        forged=json.loads(packets[0])
        forged["t"]=True  # bool must not silently alias numeric zero or one
        receiver=OpticalAssembler()
        state=receiver.ingest([json.dumps(forged)])
        self.assertIsNone(state["sequence"])
        self.assertEqual(receiver.stored_chars,0)
        verified=receiver.ingest(packets)
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["payload"],original)

    def test_mixed_literal_and_unmarked_qr_packets_are_rejected_early(self):
        import json
        import hashlib

        original={
            "sequence":"QLITERALHDMI2","ok":True,
            "data":{"results":[{"value":"original"}],
                    "primary":{"$ec_ref":"results[0]"},
                    "transport_compacted":True,
                    "text":"".join(hashlib.sha256(str(i).encode()).hexdigest()
                                   for i in range(90))}}
        packets=encode_result_chunks(original,chunk_chars=120)
        self.assertGreaterEqual(len(packets),2)
        receiver=OpticalAssembler()
        receiver.ingest(packets[:1])
        self.assertEqual(receiver.stored_chars,len(packets[0]))
        foreign=json.loads(packets[1])
        del foreign["t"]
        partial=receiver.ingest([json.dumps(foreign)])
        self.assertEqual(partial["received"],1)
        self.assertTrue(receiver.ingest(packets[1:])["verified"])
        self.assertEqual(receiver.ingest([])["payload"],original)

    def test_wrong_ec2_version_cannot_poison_first_assembler_slot(self):
        import json
        from easychange.core.result import Result

        packets=encode_result_chunks(
            Result(True,"read",{"text":"good"},sequence="QASMVER2026"))
        original=json.loads(packets[0])
        for unsupported in (None, True, False, 1, 3, "2", 2.0):
            altered=dict(original)
            if unsupported is None:
                altered.pop("v")
            else:
                altered["v"]=unsupported
            with self.subTest(version=unsupported):
                assembly=OpticalAssembler()
                pending=assembly.ingest([json.dumps(altered)])
                self.assertFalse(pending["complete"])
                self.assertIsNone(pending["sequence"])
                self.assertEqual(assembly.chunks,{})
                recovered=assembly.ingest(packets)
                self.assertTrue(recovered["verified"])
                self.assertEqual(recovered["payload"]["sequence"],"QASMVER2026")

    def test_invalid_transfer_hash_metadata_cannot_reserve_qr_slot(self):
        import json

        original={"sequence":"QIDENTITY01","ok":True,
                  "data":{"text":"authenticated receipt"}}
        packets=encode_result_chunks(original)
        valid=json.loads(packets[0])
        cases=(
            ("h", None),
            ("h", "g"*16),
            ("h", "f"*15),
            ("c", None),
            ("c", "g"*8),
            ("c", "a"*7),
            ("s", ""),
            ("s", "Q"*257),
        )
        for field,invalid in cases:
            with self.subTest(field=field,invalid=invalid):
                altered=dict(valid)
                altered[field]=invalid
                receiver=OpticalAssembler()
                pending=receiver.ingest([json.dumps(altered)])
                self.assertFalse(pending["complete"])
                self.assertIsNone(pending["sequence"])
                self.assertEqual(pending["received"],0)
                self.assertEqual(receiver.stored_chars,0)
                verified=receiver.ingest(packets)
                self.assertTrue(verified["complete"])
                self.assertTrue(verified["verified"])
                self.assertEqual(verified["payload"]["sequence"],"QIDENTITY01")

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
