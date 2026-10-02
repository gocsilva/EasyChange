from easychange.core.result import Result
from easychange.remote.optical_protocol import (
    adaptive_chunk_chars, decode_result_chunks, encode_result_chunks, encode_result_header,
)


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
    bad["x"] = f"{binascii.crc32(bad["d"].encode("ascii")) & 0xffffffff:08x}"
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
