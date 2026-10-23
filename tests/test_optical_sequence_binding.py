"""Protect EC1/EC2 correlation against QR envelope sequence relabeling."""
from __future__ import annotations

import json

import pytest

from easychange.remote.optical_protocol import (
    decode_result_chunks, encode_result_chunks,
)
from mcp_adapter.optical_reader import OpticalAssembler


def test_relabeling_qr_sequence_does_not_verify_someone_elses_receipt():
    packets = encode_result_chunks({
        "sequence": "QORIGINAL", "ok": True,
        "data": {"status": "completed", "content": "immutable"},
    })
    relabeled = []
    for raw in packets:
        packet = json.loads(raw)
        packet["s"] = "QSPOOF"
        relabeled.append(json.dumps(packet, separators=(",", ":")))
    # The content hash is still correct. Only the unauthenticated outer
    # request identity was changed.
    with pytest.raises(ValueError, match="optical result sequence mismatch"):
        decode_result_chunks(relabeled)

    assembler = OpticalAssembler()
    with pytest.raises(ValueError, match="optical result sequence mismatch"):
        assembler.ingest(relabeled)
    assert assembler.sequence == ""
    assert assembler.chunks == {}

    recovered = assembler.ingest(packets)
    assert recovered["complete"] is True
    assert recovered["verified"] is True
    assert recovered["sequence"] == "QORIGINAL"
    assert recovered["payload"]["sequence"] == "QORIGINAL"


@pytest.mark.parametrize("value", [None, "", "absent"])
def test_unsequenced_legacy_ec2_uses_dash_sentinel(value):
    result = {"ok": True, "data": {"value": "read-only"}}
    if value != "absent":
        result["sequence"] = value
    packets = encode_result_chunks(result)
    assert all(json.loads(packet)["s"] == "-" for packet in packets)
    assert decode_result_chunks(packets) == result
    assembled = OpticalAssembler().ingest(packets)
    assert assembled["complete"] is True
    assert assembled["sequence"] == "-"
    assert assembled["payload"] == result
