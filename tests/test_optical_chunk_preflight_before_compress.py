"""Invalid EC2 recovery requests must not serialize/compress durable payloads."""
import json

import pytest

import easychange.remote.optical_protocol as optical


@pytest.mark.parametrize("indexes,exception,error", [
    ([], ValueError, "at least one optical chunk"),
    ([-1], IndexError, "OPTICAL_CHUNK_OUT_OF_RANGE"),
    ([True], ValueError, "invalid optical chunk index"),
    ([None], ValueError, "invalid optical chunk index"),
    (["x"], ValueError, "invalid optical chunk index"),
    (["9" * 33], ValueError, "invalid optical chunk index"),
])
def test_invalid_indexes_rejected_before_expensive_preparation(
    indexes, exception, error, monkeypatch,
):
    attempts = []

    def forbid_prepare(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("UNNECESSARY_EC2_COMPRESSION")

    monkeypatch.setattr(optical, "prepare_optical_result", forbid_prepare)
    with pytest.raises(exception, match=error):
        optical.encode_result_chunks(
            {"sequence": "QRECOVERY", "ok": True}, indexes=indexes,
        )
    assert attempts == []


def test_valid_sparse_indexes_keep_packet_identity_and_order():
    result = {"sequence": "QSPARSEVALID", "ok": True,
              "data": {"values": list(range(1000))}}
    prepared = optical.prepare_optical_result(result, chunk_chars=120)
    assert prepared.total >= 3
    packets = optical.encode_result_chunks(
        result, indexes=[" 2 ", 0, 2, 1], prepared=prepared,
    )
    parsed = [json.loads(packet) for packet in packets]
    assert [entry["i"] for entry in parsed] == [2, 0, 1]
    assert len({entry["h"] for entry in parsed}) == 1
    assert len({entry["c"] for entry in parsed}) == 1
