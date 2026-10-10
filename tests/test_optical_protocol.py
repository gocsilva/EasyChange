import json

from easychange.core.result import Result
from easychange.remote.optical_protocol import (
    adaptive_chunk_chars, decode_result_chunks, encode_durable_result_header,
    encode_result_chunks, encode_result_header,
)


def test_ec2_encoder_caps_utf8_json_bytes_before_compression(monkeypatch):
    import pytest
    from unittest.mock import patch
    import easychange.remote.optical_protocol as optical

    monkeypatch.setattr(optical,"MAX_EC2_RESULT_BYTES",180)
    # UTF-8 multibyte characters are larger than their Python char count.
    oversized={"data":"é"*150}
    with patch.object(optical.zlib,"compress",
                      side_effect=AssertionError("ENCODED_TOO_LATE")) as compressed:
        with pytest.raises(ValueError,match="OPTICAL_RESULT_TOO_LARGE"):
            optical._encode_raw(oversized)
    compressed.assert_not_called()

    valid={"data":"ready"}
    raw, zipped, token=optical._encode_raw(valid)
    assert json.loads(raw)==valid
    assert zipped
    assert isinstance(token,str)


def test_ec2_encoder_refuses_huge_single_token_without_encoding_second_copy(monkeypatch):
    import pytest
    from unittest.mock import patch
    import easychange.remote.optical_protocol as optical

    class UntrustedToken(str):
        def encode(self,*_args,**_kwargs):
            raise AssertionError("HUGE_INTERMEDIATE_UTF8_COPY")

    class HugeSingleTokenEncoder:
        def iterencode(self,_value):
            yield UntrustedToken("x"*1024)

    monkeypatch.setattr(optical,"MAX_EC2_RESULT_BYTES",64)
    with patch.object(optical.json,"JSONEncoder",
                      return_value=HugeSingleTokenEncoder()):
        with pytest.raises(ValueError,match="OPTICAL_RESULT_TOO_LARGE"):
            optical._encode_raw({"data":"not-reached"})


def test_ec2_long_json_string_preserves_exact_original_utf8_wire_bytes():
    import easychange.remote.optical_protocol as optical

    value={"data":"çá🧪"*3200,"sequence":"QENCODEWIDE"}
    expected=json.dumps(value,ensure_ascii=False,separators=(",",":")).encode("utf-8")
    raw,compressed,token=optical._encode_raw(value)
    assert raw==expected
    assert compressed
    assert token


def test_ec2_encoder_checks_compressed_and_base64_budgets(monkeypatch):
    import pytest
    from unittest.mock import patch
    import easychange.remote.optical_protocol as optical

    value={"data":"small and valid"}
    monkeypatch.setattr(optical,"MAX_EC2_COMPRESSED_BYTES",1)
    with patch.object(optical.base64,"urlsafe_b64encode",
                      side_effect=AssertionError("BASE64_TOO_EARLY")) as encoded:
        with pytest.raises(ValueError,match="OPTICAL_COMPRESSED_RESULT_TOO_LARGE"):
            optical._encode_raw(value)
    encoded.assert_not_called()

    monkeypatch.setattr(optical,"MAX_EC2_COMPRESSED_BYTES",12*1024*1024)
    monkeypatch.setattr(optical,"MAX_EC2_TOKEN_CHARS",1)
    with pytest.raises(ValueError,match="OPTICAL_RESULT_TOKEN_TOO_LARGE"):
        optical._encode_raw(value)


def test_ec2_round_trip_large_read_payload():
    result = Result(True, "read", {
        "path": "src/Foo.cs",
        "start": 1,
        "total_lines": 600,
        "lines": [f"{i}|public string Value{i} {{ get; set; }}" for i in range(1, 401)],
    }, command_id="C10", duration_ms=7, sequence="QOPT1234")
    packets = encode_result_chunks(result, chunk_chars=240)
    assert len(packets) > 4
    decoded = decode_result_chunks(packets)
    assert decoded["sequence"] == "QOPT1234"
    assert decoded["data"]["lines"][399].startswith("400|")
    assert all(len(packet.encode("utf-8")) < 600 for packet in packets)


def test_compact_large_result_advertises_optical_transport():
    result = Result(True, "read", {"lines": ["x" * 200 for _ in range(20)]}, sequence="QOPT9999")
    rendered = result.render("compact")
    assert rendered.startswith("EC1 QOPT9999 OK")
    assert '"optical":"EC2"' in rendered
    assert len(rendered) < 1200



def test_ec2_total_qr_budget_rejects_excessive_duplicate_text(monkeypatch):
    import pytest
    import easychange.remote.optical_protocol as optical

    # A small legitimate result remains decodable. Repeated QR frames count
    # toward the total input budget, even if their token is a duplicate.
    packets=encode_result_chunks(
        {"sequence":"QINPUTCAP1","ok":True,"data":{"text":"hello"}},
        chunk_chars=10000)
    assert len(packets)==1
    size=len(packets[0])
    monkeypatch.setattr(optical,"MAX_EC2_PACKET_SET_CHARS",size*2-1)
    assert decode_result_chunks(packets)["sequence"]=="QINPUTCAP1"
    with pytest.raises(ValueError,match="optical packet set too large"):
        decode_result_chunks([packets[0],packets[0]])


def test_ec2_total_input_budget_is_checked_before_json_decode(monkeypatch):
    import pytest
    from unittest.mock import patch
    import easychange.remote.optical_protocol as optical

    monkeypatch.setattr(optical,"MAX_EC2_PACKET_SET_CHARS",8)
    with patch.object(optical.json,"loads",
                      side_effect=AssertionError("JSON_PARSED_BEFORE_SIZE_CHECK")) as parser:
        with pytest.raises(ValueError,match="optical packet set too large"):
            decode_result_chunks(['{"padding":12345}'])
    parser.assert_not_called()


def test_ec2_duplicate_chunks_are_idempotent():
    import json

    result = Result(True, "read", {"value": "x" * 2000}, sequence="QDUP1234")
    packets = encode_result_chunks(result, chunk_chars=180)
    duplicated = packets + [packets[0], packets[-1]]
    decoded = decode_result_chunks(duplicated)
    assert decoded["sequence"] == "QDUP1234"
    assert decoded["data"]["value"] == "x" * 2000
    first = json.loads(packets[0])
    assert first["v"] == 2
    assert first["p"] == "EC2"
    assert "rt" not in first
    assert "st" not in first
    assert "e" not in first


def test_ec2_conflicting_duplicate_chunk_is_rejected():
    import json
    import pytest

    result = Result(True, "read", {"value": "x" * 2000}, sequence="QDUP5678")
    packets = encode_result_chunks(result, chunk_chars=180)
    bad = json.loads(packets[0])
    bad["d"] = bad["d"] + "A"
    import binascii
    bad["x"] = f"{binascii.crc32(bad['d'].encode('ascii')) & 0xffffffff:08x}"
    with pytest.raises(ValueError, match="conflicting duplicate"):
        decode_result_chunks(packets + [json.dumps(bad, separators=(",", ":"))])



def test_ec2_v2_compact_envelope_reduces_qr_packet_overhead():
    import json

    result = Result(True, "read", {"value": "x" * 3000}, sequence="QPACKET1")
    packets = encode_result_chunks(result, chunk_chars=180)
    packet = json.loads(packets[0])
    assert set(packet) == {"p", "v", "s", "i", "n", "h", "c", "x", "d"}
    assert len(packets[0]) < 310
    assert decode_result_chunks(packets)["sequence"] == "QPACKET1"


def test_ec2_rejects_invalid_base64url_even_with_recalculated_crc():
    import binascii
    import pytest

    packet=json.loads(encode_result_chunks(
        Result(True,"read",{"value":"verified"},sequence="QSTRICTURL"))[0])
    packet["d"]="!"+packet["d"]
    packet["x"]=f"{binascii.crc32(packet['d'].encode('ascii')) & 0xffffffff:08x}"
    with pytest.raises(ValueError,match="invalid optical base64url fragment"):
        decode_result_chunks([json.dumps(packet)])


def test_ec2_fragment_crc_rejects_corruption():
    import json
    import pytest

    result = Result(True, "read", {"value": "abcdef" * 500}, sequence="QCRC1234")
    packets = encode_result_chunks(result, chunk_chars=180)
    broken = json.loads(packets[0])
    broken["d"] = broken["d"][:-1] + ("A" if broken["d"][-1] != "A" else "B")
    corrupted = [json.dumps(broken, separators=(",", ":")), *packets[1:]]
    with pytest.raises(ValueError, match="chunk crc32 mismatch"):
        decode_result_chunks(corrupted)


def test_ec2_rejects_ambiguous_packet_fields_and_nonfinite_json():
    import pytest
    from easychange.remote.optical_protocol import decode_ec2_packet_json

    valid=encode_result_chunks(
        Result(True,"read",{"text":"unmodified"},sequence="QSTRICTJSON1"))[0]
    assert decode_result_chunks([valid])["data"]["text"]=="unmodified"
    for suffix in (
            ',"n":999',
            ',"p":"EC2H"',
            ',"metadata":{"id":1,"id":2}',
            ',"metadata":{"number":NaN}',
            ',"metadata":{"number":Infinity}',
            ',"metadata":{"number":1e999999}'):
        tampered=valid[:-1]+suffix+"}"
        with pytest.raises(ValueError,match="invalid optical packet JSON"):
            decode_result_chunks([tampered])
        with pytest.raises(ValueError,match="invalid optical packet JSON"):
            decode_ec2_packet_json(tampered)
        # A corrupted QR must not alter a later, valid transfer.
        assert decode_result_chunks([valid])["sequence"]=="QSTRICTJSON1"


def test_ec2_rejects_pathologically_nested_packet_json():
    import pytest

    nested="["*2000+"]"*2000
    with pytest.raises(ValueError,match="invalid optical packet JSON"):
        decode_result_chunks([nested])


def test_durable_ec2_header_requires_boolean_finished_true():
    import pytest
    from easychange.remote.optical_protocol import encode_durable_result_header

    metadata={"sequence":"QSTRICT1","finished":"true",
              "payload_length":12,"success":True,"mutation":False}
    with pytest.raises(ValueError,match="durable result is not terminal"):
        encode_durable_result_header(metadata)
    metadata["finished"]=True
    valid=json.loads(encode_durable_result_header(metadata))
    assert valid["f"] is True


def test_ec2_direct_decoder_rejects_invalid_transfer_identity_early():
    import pytest

    original=Result(True,"read",{"text":"valid"},sequence="QIDENTITY02")
    packets=encode_result_chunks(original)
    valid=json.loads(packets[0])
    for field,value in (("h",None),("h",1),("h","g"*16),
                        ("c",False),("c","g"*8),("s","")):
        with pytest.raises(ValueError,match="invalid optical transfer identity"):
            forged=dict(valid)
            forged[field]=value
            decode_result_chunks([json.dumps(forged)])


def test_ec2_rejects_non_v2_envelopes_and_never_normalizes_bool_version():
    import json
    import pytest
    from easychange.core.result import Result

    packets=encode_result_chunks(
        Result(True,"read",{"text":"versioned"},sequence="QVER2026"))
    assert decode_result_chunks(packets)["sequence"]=="QVER2026"
    packet=json.loads(packets[0])
    for invalid in (None, True, False, "2", 1, 3, 2.0, [], {}):
        altered=dict(packet)
        if invalid is None:
            altered.pop("v")
        else:
            altered["v"]=invalid
        with pytest.raises(ValueError,match="invalid optical transfer identity"):
            decode_result_chunks([json.dumps(altered)])


def test_ec2_decoder_requires_real_crc_for_every_fragment():
    import json
    import pytest
    from easychange.core.result import Result

    packets=encode_result_chunks(
        Result(True,"read",{"data":"crc contract"},sequence="QCRC2026"))
    assert packets
    assert decode_result_chunks(packets)["sequence"]=="QCRC2026"
    original=json.loads(packets[0])
    for missing in (None, "", False, 0, "0000000", "00000000",
                    "fffffffff", "FFFFFFFF", [], {}):
        altered=dict(original)
        if missing is None:
            altered.pop("x")
        else:
            altered["x"]=missing
        with pytest.raises(ValueError,match="optical chunk crc32 mismatch"):
            decode_result_chunks([json.dumps(altered)])


def test_ec2_rejects_untrusted_packet_counts_and_invalid_indexes():
    import pytest
    result=Result(True,"read",{"text":"ok"},sequence="QBADCOUNT")
    original=encode_result_chunks(result)
    packet=json.loads(original[0])
    packet["n"]=10**9
    with pytest.raises(ValueError, match="invalid optical packet count"):
        decode_result_chunks([json.dumps(packet)])
    packet["n"]=1
    packet["i"]=10**9
    with pytest.raises(ValueError, match="invalid optical chunk index"):
        decode_result_chunks([json.dumps(packet)])
    packet["i"]=True
    with pytest.raises(ValueError, match="invalid optical chunk index"):
        decode_result_chunks([json.dumps(packet)])


def test_ec2_bounded_decompression_rejects_small_compressed_bomb(monkeypatch):
    import pytest
    import easychange.remote.optical_protocol as protocol

    result=Result(True,"read",{"text":"Z"*6000},sequence="QBOMB001")
    packets=encode_result_chunks(result)
    monkeypatch.setattr(protocol,"MAX_EC2_RESULT_BYTES",1024)
    with pytest.raises(ValueError,match="optical decompressed result too large"):
        decode_result_chunks(packets)


def test_ec2_rejects_trailing_or_incomplete_zlib_stream():
    import base64
    import binascii
    import pytest

    result=Result(True,"read",{"text":"ok"},sequence="QBADZLIB")
    packets=encode_result_chunks(result)
    assert len(packets)==1
    original=json.loads(packets[0])
    token=original["d"]
    zipped=base64.urlsafe_b64decode(token+"="*(-len(token)%4))
    for corrupt in (zipped+b"TRAILING", zipped[:-2]):
        packet=dict(original)
        packet["d"]=base64.urlsafe_b64encode(corrupt).decode("ascii").rstrip("=")
        packet["x"]=f"{binascii.crc32(packet['d'].encode('ascii')) & 0xffffffff:08x}"
        with pytest.raises(ValueError,match="invalid optical compressed stream"):
            decode_result_chunks([json.dumps(packet)])


def test_ec2_status_header_contains_small_definitive_manifest():
    import json

    result = Result(
        True,
        "structured",
        {
            "mutation_receipt": {
                "state": "COMMITTED",
                "lifecycle_state": "APPLIED",
            },
            "value": "x" * 5000,
        },
        command_id="C77",
        sequence="QHEADER77",
    )
    chunks = encode_result_chunks(result)
    header = json.loads(encode_result_header(result, chunk_count=len(chunks)))
    assert header["p"] == "EC2H"
    assert header["s"] == "QHEADER77"
    assert header["f"] is True
    assert header["ok"] is True
    assert header["n"] == len(chunks)
    assert header["m"] == "APPLIED"
    assert header["cid"] == "C77"
    assert len(header["h"]) == 64
    assert len(json.dumps(header, separators=(",", ":"))) < 260


def test_adaptive_chunk_sizing_reduces_small_payload_chunk_count():
    result = Result(
        True,
        "read",
        {"lines": [f"line-{i:04d}-{i * 7919:08d}" for i in range(200)]},
        sequence="QADAPT1",
    )
    adaptive = encode_result_chunks(result)
    fixed = encode_result_chunks(result, chunk_chars=180)
    assert adaptive_chunk_chars(1000) == 260
    assert adaptive_chunk_chars(3000) == 220
    assert adaptive_chunk_chars(8000) == 180
    assert len(adaptive) <= len(fixed)


def test_ec2_does_not_erase_boolean_vs_integer_differences():
    import json
    from easychange.remote.optical_protocol import (
        _compact_transport_value, encode_result_chunks, decode_result_chunks,
    )

    result={
        "ok":True,"sequence":"QTYPESTRICT1",
        "data":{"results":[{"value":1,"nested":[False,2]}],
                "primary":{"value":True,"nested":[0,2]}}
    }
    # Python compares True == 1 and False == 0. The wire format must not.
    assert _compact_transport_value(result) is result
    packets=encode_result_chunks(result)
    assert "t" not in json.loads(packets[0])
    decoded=decode_result_chunks(packets)
    assert type(decoded["data"]["primary"]["value"]) is bool
    assert type(decoded["data"]["results"][0]["value"]) is int
    assert type(decoded["data"]["primary"]["nested"][0]) is bool
    assert type(decoded["data"]["results"][0]["nested"][0]) is int
    assert decoded["data"]["primary"]["nested"][0] is False


def test_ec2_caller_owned_transport_markers_are_never_interpreted_as_references():
    import json
    from easychange.remote.optical_protocol import (
        _compact_transport_value, encode_result_chunks, decode_result_chunks,
        encode_result_bundle,
    )

    caller={
        "ok":True,"sequence":"QLITERALREF1",
        "data":{
            "results":[{"value":"original result"}],
            "primary":{"$ec_ref":"results[0]"},
            "transport_compacted":True,
        },
    }
    assert _compact_transport_value(caller) is caller
    frames=encode_result_chunks(caller)
    assert json.loads(frames[0])["t"] == 0
    assert decode_result_chunks(frames)==caller
    bundle,header=encode_result_bundle(caller)
    assert json.loads(bundle[0])["t"] == 0
    assert decode_result_chunks(bundle)==caller
    assert json.loads(header)["s"]=="QLITERALREF1"


def test_ec2_structural_compaction_handles_cycles_without_spinning():
    from easychange.remote.optical_protocol import _json_equal_with_types
    left={}
    right={}
    left["self"]=left
    right["self"]=right
    assert _json_equal_with_types(left,right)
    assert not _json_equal_with_types({"1":1},{1:1})


def test_structured_primary_duplication_is_compacted_on_wire_and_restored():
    import json
    from easychange.remote.optical_protocol import (
        decode_result_chunks,
        encode_result_chunks,
        _compact_transport_value,
    )

    child = {
        "ok": True,
        "command": "discover_many",
        "data": {"payload": "X" * 6000, "items": list(range(40))},
        "error": None,
        "code": None,
        "command_id": "C1",
        "duration_ms": 1,
        "sequence": None,
    }
    result = {
        "ok": True,
        "command": "structured",
        "data": {
            "schema": "easychange.structured/2",
            "operation": "operation",
            "results": [child],
            "primary": child,
            "count": 1,
        },
        "error": None,
        "code": None,
        "command_id": "C2",
        "duration_ms": 2,
        "sequence": "QCOMPACT1",
    }

    compact = _compact_transport_value(result)
    full_bytes = len(json.dumps(result, separators=(",", ":")).encode("utf-8"))
    compact_bytes = len(json.dumps(compact, separators=(",", ":")).encode("utf-8"))
    assert compact["data"]["primary"] == {"$ec_ref": "results[0]"}
    assert compact_bytes < full_bytes * 0.65

    chunks = encode_result_chunks(result)
    restored = decode_result_chunks(chunks)
    assert restored == result



def test_ec2_sparse_retransmission_deduplicates_many_indexes_linearly():
    import hashlib

    # Deterministic low-compressibility content yields many actual packets.
    text="".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(100))
    result=Result(True,"read",{"text":text},sequence="QLINEARIDX")
    full=encode_result_chunks(result,chunk_chars=120)
    assert len(full)>10
    last=len(full)-1
    requested=([0]*3000)+([last]*3000)+(["0"]*3000)
    subset=encode_result_chunks(result,chunk_chars=120,indexes=requested)
    assert subset==[full[0],full[-1]]


def test_ec2_sparse_index_input_budget_checked_before_iteration(monkeypatch):
    import pytest
    import easychange.remote.optical_protocol as optical

    result=Result(True,"read",{"text":"small"},sequence="QINDEXCAP")
    monkeypatch.setattr(optical,"MAX_EC2_PACKETS",3)
    with pytest.raises(ValueError,match="OPTICAL_CHUNK_INDEX_REQUEST_TOO_LARGE"):
        encode_result_chunks(result,indexes=[0,0,0,0])
    with pytest.raises(ValueError,match="OPTICAL_CHUNK_INDEX_REQUEST_TOO_LARGE"):
        encode_result_chunks(result,indexes=iter([0]))
    with pytest.raises(ValueError,match="invalid optical chunk index"):
        encode_result_chunks(result,indexes=["0"*100000])
    assert len(encode_result_chunks(result,indexes=[0,0]))==1


def test_ec2_sparse_indexes_reject_fractional_values_without_rounding():
    import pytest

    receipt=Result(True,"read",{"payload":"example"},sequence="QINDEXEC2")
    for invalid in (0.9,1.9,True,None,[],{}):
        with pytest.raises(ValueError,match="invalid optical chunk index"):
            encode_result_chunks(receipt,indexes=[invalid])
    packet=encode_result_chunks(receipt,indexes=["0"])
    assert len(packet)==1
    assert json.loads(packet[0])["i"]==0


def test_ec2_sparse_indexes_materialize_only_requested_packets():
    import json

    result = Result(
        True,
        "read",
        {"text": "".join(
            f"row-{i:05d}-{(i * 2654435761) & 0xffffffff:08x};"
            for i in range(3000)
        )},
        sequence="QSPARSE1",
    )
    full = encode_result_chunks(result, chunk_chars=120)
    selected = encode_result_chunks(
        result,
        chunk_chars=120,
        indexes=[0, 7, len(full) - 1],
    )

    decoded = [json.loads(packet) for packet in selected]
    assert [packet["i"] for packet in decoded] == [0, 7, len(full) - 1]
    assert all(packet["n"] == len(full) for packet in decoded)
    assert selected == [full[0], full[7], full[-1]]


def test_ec2_bundle_preserves_full_count_and_advertises_broadcast_window():
    import json
    from easychange.remote.optical_protocol import encode_result_bundle

    result = Result(
        True,
        "read",
        {"text": "".join(
            f"value-{i:05d}-{(i * 11400714819323198485) & 0xffffffffffffffff:016x};"
            for i in range(5000)
        )},
        sequence="QBUNDLE1",
    )
    full = encode_result_chunks(result, chunk_chars=120)
    packets, header_text = encode_result_bundle(
        result,
        chunk_chars=120,
        max_packets=31,
    )
    header = json.loads(header_text)

    assert packets == full[:31]
    assert header["p"] == "EC2H"
    assert header["n"] == len(full)
    assert header["w"] == min(31, len(full))
    assert header["s"] == "QBUNDLE1"



def test_optical_transport_uses_balanced_compression_level():
    import easychange.remote.optical_protocol as protocol
    assert protocol.OPTICAL_COMPRESSION_LEVEL == 3


def test_optical_level3_roundtrip_remains_lossless():
    from easychange.core.result import Result
    from easychange.remote.optical_protocol import decode_result_chunks, encode_result_chunks

    result = Result(
        True,
        "study",
        {"text": "payload-" * 5000},
        sequence="QLEVEL3",
    )
    chunks = encode_result_chunks(result)
    decoded = decode_result_chunks(chunks)
    assert decoded["sequence"] == "QLEVEL3"
    assert decoded["data"]["text"] == "payload-" * 5000



def test_ec2_bundle_uses_compact_overflow_window_only_when_result_exceeds_cap():
    from easychange.core.result import Result
    from easychange.remote.optical_protocol import encode_result_bundle, encode_result_chunks

    result = Result(
        True,
        "read",
        {"text": "".join(
            f"value-{i:05d}-{(i * 11400714819323198485) & 0xffffffffffffffff:016x};"
            for i in range(5000)
        )},
        sequence="QOVERFLOW1",
    )
    full = encode_result_chunks(result, chunk_chars=120)
    packets, header_text = encode_result_bundle(
        result,
        chunk_chars=120,
        max_packets=15,
        overflow_packets=3,
    )
    header = json.loads(header_text)

    assert len(full) > 15
    assert packets == full[:3]
    assert header["n"] == len(full)
    assert header["w"] == 3


def test_ec2_bundle_keeps_all_small_result_chunks_with_overflow_policy():
    from easychange.core.result import Result
    from easychange.remote.optical_protocol import encode_result_bundle, encode_result_chunks

    result = Result(
        True,
        "state",
        {"value": "small"},
        sequence="QSMALL1",
    )
    full = encode_result_chunks(result, chunk_chars=120)
    assert len(full) <= 15
    packets, header_text = encode_result_bundle(
        result,
        chunk_chars=120,
        max_packets=15,
        overflow_packets=3,
    )
    header = json.loads(header_text)

    assert packets == full
    assert header["w"] == len(full)



def test_durable_terminal_header_is_payload_free_and_marks_durable_only():
    payload = encode_durable_result_header({
        "sequence": "QFASTHDR1",
        "finished": True,
        "success": True,
        "return_code": None,
        "payload_length": 123456,
        "sha256": "a" * 64,
        "mutation": True,
        "mutation_state": "APPLIED",
        "command_id": "CFAST1",
    })
    header = json.loads(payload)
    assert header == {
        "p": "EC2H", "v": 1, "s": "QFASTHDR1", "f": True,
        "ok": True, "rc": None, "l": 123456, "n": 0,
        "h": "a" * 64, "m": "APPLIED", "cid": "CFAST1",
        "w": 0, "d": 1,
    }
