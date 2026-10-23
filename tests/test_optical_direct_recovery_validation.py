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
    return {"p": "EC2", "s": sequence, "i": index, "n": total, "d": "test"}


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
