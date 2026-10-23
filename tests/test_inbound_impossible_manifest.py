"""Reject inbound transfer manifests that no sequence of chunks can satisfy."""
from __future__ import annotations

import hashlib
import json

import pytest

from easychange.core.inbound_transfer import InboundCommandStore, InboundTransferError


@pytest.mark.parametrize("compressed_bytes,chunk_count", [
    (1, 2),           # each declared chunk must contain at least one byte
    (8193, 1),        # a single chunk is capped at 8192 bytes
    (16385, 2),       # two chunks cannot represent more than 16384 bytes
])
def test_impossible_inbound_manifest_rejected_without_reserving_slot(
    tmp_path, compressed_bytes, chunk_count,
):
    store = InboundCommandStore(tmp_path / "inbound")
    with pytest.raises(InboundTransferError) as exc:
        store.begin(
            "UIMPOSSIBLE01", raw_bytes=20,
            compressed_bytes=compressed_bytes, chunk_count=chunk_count,
            sha256=hashlib.sha256(b"test").hexdigest(),
        )
    assert exc.value.code == "INBOUND_COMPRESSED_CHUNK_LAYOUT_INVALID"
    assert not (store.root / "UIMPOSSIBLE01").exists()


@pytest.mark.parametrize("compressed_bytes,chunk_count", [
    (1, 2), (8193, 1),
])
def test_impossible_persisted_manifest_rejected_before_status_scan(
    tmp_path, compressed_bytes, chunk_count, monkeypatch,
):
    store = InboundCommandStore(tmp_path / "inbound")
    transfer_id = "UIMPOSSIBLE02"
    store.begin(
        transfer_id, raw_bytes=20, compressed_bytes=10, chunk_count=1,
        sha256=hashlib.sha256(b"test").hexdigest(),
    )
    path = store._meta_path(transfer_id)
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update(compressed_bytes=compressed_bytes, chunk_count=chunk_count)
    path.write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(store, "_missing_locked", lambda *_: (_ for _ in ()).throw(
        AssertionError("impossible manifest reached expensive chunk scan")
    ))
    with pytest.raises(InboundTransferError) as exc:
        store.status(transfer_id)
    assert exc.value.code == "INBOUND_TRANSFER_META_CORRUPT"
