"""Optical header polling does not redo identical chunk-size work."""
from __future__ import annotations

import pytest

from easychange.core.result_store import DurableResultStore


def test_header_validates_chunk_size_once_for_cache_hit_and_miss(tmp_path, monkeypatch):
    store = DurableResultStore(tmp_path / "receipts")
    calls = []
    original = DurableResultStore._validated_chunk_size

    def tracked(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(
        DurableResultStore, "_validated_chunk_size", staticmethod(tracked)
    )

    assert store.header("QEMPTY", chunk_bytes="512") is None
    assert calls == ["512"]

    store.mark_running("QHEADER", "a" * 64, ":status", mutation=False)
    store.complete("QHEADER", "a" * 64, ":status", {
        "ok": True, "command": "status", "data": {"message": "ok"}
    })
    calls.clear()
    cached = store.header("QHEADER", chunk_bytes="512")
    assert cached["finished"] is True
    assert cached["chunk_count"] >= 1
    assert calls == ["512"]

    store._terminal_header_cache.clear()
    calls.clear()
    cold = store.header("QHEADER", chunk_bytes="512")
    assert cold["chunk_count"] == cached["chunk_count"]
    assert cold["sha256"] == cached["sha256"]
    assert calls == ["512"]


def test_invalid_chunk_size_fails_before_opening_receipt(tmp_path, monkeypatch):
    store = DurableResultStore(tmp_path / "receipts")

    def do_not_access_disk(_sequence):
        raise AssertionError("INVALID_CHUNK_SIZE_REACHED_DISK")

    monkeypatch.setattr(store, "get", do_not_access_disk)
    with pytest.raises(ValueError, match="INVALID_RESULT_CHUNK_BYTES"):
        store.header("QHEADER", chunk_bytes=object())
