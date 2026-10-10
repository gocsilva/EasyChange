"""EC2 stream compression respects the RAM budget and avoids repeated work."""
from __future__ import annotations

import hashlib
import zlib

import easychange.core.result_store as result_store
from easychange.core.result_store import DurableResultStore


def _completed_store(tmp_path, sequence, body):
    store = DurableResultStore(tmp_path / "receipts")
    store.mark_running(sequence, "a" * 64, ":state", mutation=False)
    store.complete(
        sequence, "a" * 64, ":state",
        {"ok": True, "command": "state",
         "data": {"content": body}, "sequence": sequence},
    )
    return store


def test_ec2_compression_enforces_bytes_before_allocating_full_stream(tmp_path, monkeypatch):
    # Pseudorandom SHA-256 text is far larger after compression than the
    # deliberately tiny budget, but its JSON receipt is entirely valid.
    text = "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(150))
    store = _completed_store(tmp_path, "QSTREAMCAP1", text)
    monkeypatch.setattr(result_store, "MAX_COMPRESSED_RESULT_STREAM_BYTES", 128)
    invocations = []
    original = store._compress_result_stream_bounded

    def measured(raw):
        invocations.append(len(raw))
        return original(raw)

    monkeypatch.setattr(store, "_compress_result_stream_bounded", measured)
    first = store.stream_meta("QSTREAMCAP1")
    assert first["eligible"] is False
    assert first["total_chunks"] == 0
    assert first["stream_bytes"] == 0
    assert first["compression_ratio"] == 1.0
    assert len(invocations) == 1

    # A response must still distinguish an ineligible stream from a missing
    # execution, and the original uncompressed chunk transport keeps working.
    second = store.stream_chunks("QSTREAMCAP1", [0])
    assert second["eligible"] is False
    assert second["chunks"] == []
    assert store.chunk_meta("QSTREAMCAP1")["total_chunks"] > 0
    assert store.chunk("QSTREAMCAP1", 0) is not None
    assert len(invocations) == 1  # Negative cache avoids repeated compression.


def test_ec2_compressed_bytes_remain_identical_for_eligible_result(tmp_path, monkeypatch):
    store = _completed_store(tmp_path, "QSTREAMCAP2", "A" * 10000)
    monkeypatch.setattr(result_store, "MAX_COMPRESSED_RESULT_STREAM_BYTES", 65536)
    original = store._result_payload("QSTREAMCAP2")
    assert original is not None
    raw = original[1]
    payload = store._result_stream_payload("QSTREAMCAP2")
    assert payload is not None
    compressed, compressed_digest, raw_digest, raw_size = payload
    assert compressed == zlib.compress(raw, level=1)
    assert hashlib.sha256(compressed).hexdigest() == compressed_digest
    assert raw_digest == hashlib.sha256(raw).hexdigest()
    assert raw_size == len(raw)
    assert store.stream_meta("QSTREAMCAP2")["eligible"] is True
