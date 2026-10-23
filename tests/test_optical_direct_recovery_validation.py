"""Regression checks for the direct EC2 optical recovery fast path."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from easychange.ui.main_window import _direct_recovery_optical_payloads


def recovery(packets, target="QRECOVER"):
    return SimpleNamespace(
        ok=True, command="optical_chunks",
        data={"target_sequence": target, "packets": packets},
    )


def packet(index=0, total=2, sequence="QRECOVER"):
    import binascii

    data = "test"
    return {
        "p": "EC2", "v": 2, "s": sequence, "i": index, "n": total,
        "h": "a" * 16, "c": "b" * 8,
        "d": data,
        "x": f"{binascii.crc32(data.encode('ascii')) & 0xffffffff:08x}",
    }


def test_direct_recovery_preserves_original_noncontiguous_packets():
    packets = [packet(2, 6), packet(4, 6)]
    rendered = _direct_recovery_optical_payloads(recovery(packets))
    assert [json.loads(value) for value in rendered] == packets


@pytest.mark.parametrize("index,total", [
    (0.5, 2), (True, 2), ("1", 2),
    (0, 2.5), (0, True), (0, "2"),
    (-1, 2), (2, 2), (0, 0),
])
def test_direct_recovery_rejects_invalid_chunk_identity(index, total):
    assert _direct_recovery_optical_payloads(
        recovery([packet(index, total)])
    ) is None


def test_direct_recovery_rejects_mixed_sequences_and_totals():
    assert _direct_recovery_optical_payloads(recovery(
        [packet(0, 2), packet(1, 2, "QOTHER")]
    )) is None
    assert _direct_recovery_optical_payloads(recovery(
        [packet(0, 2), packet(1, 3)]
    )) is None
    assert _direct_recovery_optical_payloads(recovery(
        [packet(0, 2)], target="QOTHER"
    )) is None


def test_direct_recovery_rejects_packets_not_json_serializable():
    assert _direct_recovery_optical_payloads(recovery([
        {**packet(), "d": object()},
    ])) is None
    assert _direct_recovery_optical_payloads(recovery([
        {**packet(), "d": float("nan")},
    ])) is None



@pytest.mark.parametrize("field,value", [
    ("v", 1), ("v", True),
    ("h", "Z" * 16), ("c", "Z" * 8),
    ("x", "00000000"), ("x", None),
    ("d", "not-base64!"), ("t", True), ("t", 1),
    ("n", 200001),
])
def test_direct_recovery_rejects_unreadable_or_unsafe_packets(field, value):
    corrupt = packet()
    corrupt[field] = value
    assert _direct_recovery_optical_payloads(recovery([corrupt])) is None


def test_direct_recovery_rejects_mixed_payload_digests():
    left, right = packet(0, 2), packet(1, 2)
    right["h"] = "c" * 16
    assert _direct_recovery_optical_payloads(recovery([left, right])) is None


def test_direct_recovery_accepts_real_crc_valid_ec2_packet():
    from easychange.remote.optical_protocol import encode_result_chunks

    actual = json.loads(encode_result_chunks({
        "sequence": "QRECOVER", "ok": True,
        "data": {"text": "verified recovery"},
    })[0])
    assert _direct_recovery_optical_payloads(recovery([actual])) == [
        json.dumps(actual, ensure_ascii=False, separators=(",", ":"))
    ]
    actual["x"] = "00000000"
    assert _direct_recovery_optical_payloads(recovery([actual])) is None



def test_direct_recovery_rejects_conflicting_duplicate_index():
    import binascii

    first = packet(0, 2)
    conflicting = {**first, "d": "other"}
    conflicting["x"] = f"{binascii.crc32(b'other') & 0xffffffff:08x}"
    assert _direct_recovery_optical_payloads(
        recovery([first, conflicting])
    ) is None


def test_direct_recovery_deduplicates_identical_retransmissions():
    first = packet(0, 2)
    assert [
        json.loads(value) for value in _direct_recovery_optical_payloads(
            recovery([first, dict(first)])
        )
    ] == [first]


def test_direct_recovery_rejects_mixed_extended_identity():
    first = packet(0, 2)
    second = packet(1, 2)
    second["e"] = "incompatible"
    assert _direct_recovery_optical_payloads(
        recovery([first, second])
    ) is None
