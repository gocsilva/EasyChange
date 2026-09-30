from easychange.core.result import Result
from easychange.remote.optical_protocol import decode_result_chunks, encode_result_chunks


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
    assert first["rt"] == "result"


def test_ec2_conflicting_duplicate_chunk_is_rejected():
    import json
    import pytest

    result = Result(True, "read", {"value": "x" * 2000}, sequence="QDUP5678")
    packets = encode_result_chunks(result, chunk_chars=180)
    bad = json.loads(packets[0])
    bad["d"] = bad["d"] + "A"
    with pytest.raises(ValueError, match="conflicting duplicate"):
        decode_result_chunks(packets + [json.dumps(bad, separators=(",", ":"))])
