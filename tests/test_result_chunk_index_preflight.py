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
