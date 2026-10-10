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


@pytest.mark.parametrize("invalid", [
    "false", "true", 0, 1, None, [], {}, float("nan"),
])
def test_chunk_compression_must_be_literal_boolean_before_receipt_io(
    invalid, tmp_path, monkeypatch,
):
    store=DurableResultStore(tmp_path/"results")
    reads=[]
    def forbidden(sequence):
        reads.append(sequence)
        raise AssertionError("COMPRESS_FLAG_TRIGGERED_UNNECESSARY_RESULT_READ")
    monkeypatch.setattr(store,"_result_payload",forbidden)
    for action in (
        lambda: store.chunks("QCOMPRESS", [0], compress_payload=invalid),
        lambda: store.chunk("QCOMPRESS", 0, compress_payload=invalid),
    ):
        with pytest.raises(ValueError,match="INVALID_RESULT_CHUNK_COMPRESSION"):
            action()
    assert reads==[]


def test_literal_chunk_compression_options_preserve_absent_result(tmp_path):
    store=DurableResultStore(tmp_path/"results")
    for allowed in (True,False):
        assert store.chunks("QABSENT",[0],compress_payload=allowed) is None
        assert store.chunk("QABSENT",0,compress_payload=allowed) is None


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


@pytest.mark.parametrize("invalid", [
    True, False, float("inf"), float("nan"), "9" * 2048, "not-a-number",
])
def test_invalid_chunk_batch_limits_fail_before_spool_read(
    invalid, tmp_path, monkeypatch,
):
    store = DurableResultStore(tmp_path / "results")
    calls = []
    monkeypatch.setattr(
        store, "_result_stream_payload",
        lambda sequence: calls.append("stream") or (_ for _ in ()).throw(
            AssertionError("STREAM_READ_BEFORE_BATCH_VALIDATION")),
    )
    monkeypatch.setattr(
        store, "_result_payload",
        lambda sequence: calls.append("plain") or (_ for _ in ()).throw(
            AssertionError("PLAIN_READ_BEFORE_BATCH_VALIDATION")),
    )
    with pytest.raises(ValueError, match="INVALID_RESULT_STREAM_MAX_CHUNKS"):
        store.stream_chunks("QINVALID", [0], max_chunks=invalid)
    with pytest.raises(ValueError, match="INVALID_RESULT_MAX_CHUNKS"):
        store.chunks("QINVALID", [0], max_chunks=invalid)
    assert calls == []


def test_small_numeric_batch_limits_remain_compatible(tmp_path):
    store = DurableResultStore(tmp_path / "results")
    assert store._validated_batch_limit("3", "INVALID") == 3
    assert store._validated_batch_limit(0, "INVALID") == 1
    assert store._validated_batch_limit(999999, "INVALID") == 16
    assert store.stream_chunks("QABSENT", [0], max_chunks="3") is None
    assert store.chunks("QABSENT", [0], max_chunks="3") is None


@pytest.mark.parametrize("invalid", [
    True, False, None, float("nan"), float("inf"), "9" * 2048, "not-a-size",
])
def test_plain_chunk_size_preflight_avoids_disk_reads(
    invalid, tmp_path, monkeypatch,
):
    store = DurableResultStore(tmp_path / "results")
    read_calls = []
    def never_read(sequence):
        read_calls.append(sequence)
        raise AssertionError("RECEIPT_READ_BEFORE_SIZE_VALIDATION")
    monkeypatch.setattr(store, "_result_payload", never_read)
    for action in (
        lambda: store.chunk_meta("QPLAIN", chunk_bytes=invalid),
        lambda: store.chunks("QPLAIN", [0], chunk_bytes=invalid),
        lambda: store.chunk("QPLAIN", 0, chunk_bytes=invalid),
    ):
        with pytest.raises(ValueError, match="INVALID_RESULT_CHUNK_BYTES"):
            action()
    assert read_calls == []


def test_plain_chunk_size_clamps_short_numeric_values(tmp_path):
    store = DurableResultStore(tmp_path / "results")
    assert store._validated_chunk_size("1") == 256
    assert store._validated_chunk_size("999999") == 4096
    assert store.chunk_meta("QABSENT", chunk_bytes="1024") is None
    assert store.chunks("QABSENT", [0], chunk_bytes="1024") is None
    assert store.chunk("QABSENT", 0, chunk_bytes="1024") is None


@pytest.mark.parametrize("index", [-1, 10**9, "9" * 64])
def test_single_plain_chunk_rejects_impossible_index_before_read(
    index, tmp_path, monkeypatch,
):
    store = DurableResultStore(tmp_path / "results")
    calls = []
    def unexpected_read(_sequence):
        calls.append(True)
        raise AssertionError("UNNECESSARY_RECEIPT_READ")
    monkeypatch.setattr(store, "_result_payload", unexpected_read)
    expected = ("INVALID_RESULT_CHUNK_INDEX"
                if isinstance(index, str) else "RESULT_CHUNK_OUT_OF_RANGE")
    category = ValueError if isinstance(index, str) else IndexError
    with pytest.raises(category, match=expected):
        store.chunk("QABSENT", index)
    assert calls == []


@pytest.mark.parametrize("invalid", [
    True, False, None, float("nan"), float("inf"),
    "9" * 2048, "not-a-size",
])
def test_header_chunk_size_rejects_input_before_cache_or_receipt_read(
    invalid, tmp_path, monkeypatch,
):
    store = DurableResultStore(tmp_path / "results")
    touches = []

    def forbidden(*args, **kwargs):
        touches.append(True)
        raise AssertionError("HEADER_IO_BEFORE_CHUNK_SIZE_VALIDATION")

    monkeypatch.setattr(store, "_terminal_header_cache_get", forbidden)
    monkeypatch.setattr(store, "get", forbidden)
    with pytest.raises(ValueError, match="INVALID_RESULT_CHUNK_BYTES"):
        store.header("QINVALIDHEADER", chunk_bytes=invalid)
    assert touches == []


def test_header_chunk_size_short_numeric_input_preserves_absent_receipt(tmp_path):
    store = DurableResultStore(tmp_path / "results")
    assert store.header("QABSENTHEADER", chunk_bytes="512") is None
    assert store.header("QABSENTHEADER", chunk_bytes="999999") is None
