"""Impossible chunk indexes must not load and compress a durable result."""
from __future__ import annotations

import pytest

from easychange.core.result_store import DurableResultStore


@pytest.mark.parametrize("index",[-1,10**9])
def test_stream_chunk_index_rejected_before_decompress(index,tmp_path,monkeypatch):
    store=DurableResultStore(tmp_path/"results")
    calls=[]
    def unexpected_read(_sequence):
        calls.append(True)
        raise AssertionError("RECEIPT_READ_BEFORE_INDEX_VALIDATION")
    monkeypatch.setattr(store,"_result_stream_payload",unexpected_read)
    with pytest.raises(IndexError,match="RESULT_STREAM_CHUNK_OUT_OF_RANGE"):
        store.stream_chunks("QEARLYSTREAM",[index])
    assert calls==[]


@pytest.mark.parametrize("index",[-1,10**9])
def test_plain_chunk_index_rejected_before_read(index,tmp_path,monkeypatch):
    store=DurableResultStore(tmp_path/"results")
    calls=[]
    def unexpected_read(_sequence):
        calls.append(True)
        raise AssertionError("RECEIPT_READ_BEFORE_INDEX_VALIDATION")
    monkeypatch.setattr(store,"_result_payload",unexpected_read)
    with pytest.raises(IndexError,match="RESULT_CHUNK_OUT_OF_RANGE"):
        store.chunks("QEARLYPLAIN",[index])
    assert calls==[]


def test_well_formed_in_range_index_preserves_empty_receipt_semantics(tmp_path):
    store=DurableResultStore(tmp_path/"results")
    assert store.stream_chunks("QABSENT",[0]) is None
    assert store.chunks("QABSENT",[0]) is None


@pytest.mark.parametrize("kwargs, code", [
    ({"chunk_bytes": True}, "INVALID_RESULT_STREAM_CHUNK_BYTES"),
    ({"chunk_bytes": float("inf")}, "INVALID_RESULT_STREAM_CHUNK_BYTES"),
    ({"chunk_bytes": "9" * 2048}, "INVALID_RESULT_STREAM_CHUNK_BYTES"),
    ({"chunk_bytes": "invalid"}, "INVALID_RESULT_STREAM_CHUNK_BYTES"),
    ({"max_ratio": True}, "INVALID_RESULT_STREAM_MAX_RATIO"),
    ({"max_ratio": float("nan")}, "INVALID_RESULT_STREAM_MAX_RATIO"),
    ({"max_ratio": "1e309"}, "INVALID_RESULT_STREAM_MAX_RATIO"),
    ({"max_ratio": "9" * 2048}, "INVALID_RESULT_STREAM_MAX_RATIO"),
])
def test_invalid_stream_options_rejected_before_receipt_read(
    kwargs, code, tmp_path, monkeypatch,
):
    store = DurableResultStore(tmp_path / "results")
    calls = []
    def never_read(_sequence):
        calls.append(_sequence)
        raise AssertionError("SLOW_RESULT_READ_ON_INVALID_OPTIONS")
    monkeypatch.setattr(store, "_result_stream_payload", never_read)
    with pytest.raises(ValueError, match=code):
        store.stream_meta("QEARLYOPTIONS", **kwargs)
    with pytest.raises(ValueError, match=code):
        store.stream_chunks("QEARLYOPTIONS", [0], **kwargs)
    assert calls == []


def test_valid_stream_options_preserve_clamping_and_absent_result(tmp_path):
    store = DurableResultStore(tmp_path / "results")
    size, ratio = store._validated_stream_options("99999", "0.05")
    assert size == 4096
    assert ratio == 0.10
    size, ratio = store._validated_stream_options("1", "1.5")
    assert size == 256
    assert ratio == 1.0
    assert store.stream_meta("QABSENT", chunk_bytes="512", max_ratio="0.8") is None
    assert store.stream_chunks(
        "QABSENT", [0], chunk_bytes="512", max_ratio="0.8") is None
