"""HID inbound staging cannot exhaust memory or corrupt another writer's temporary."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
from unittest.mock import patch

import pytest

import easychange.core.inbound_transfer as inbound_module
from easychange.core.inbound_transfer import InboundCommandStore, InboundTransferError


def test_oversized_base64_chunk_is_rejected_before_decoding(tmp_path):
    store=InboundCommandStore(tmp_path / "inbound")
    store.MAX_CHUNK_BYTES=64
    max_token_chars=4*((store.MAX_CHUNK_BYTES+2)//3)+4
    with patch.object(inbound_module.base64,"urlsafe_b64decode",
                      side_effect=AssertionError("unbounded Base64 allocation")) as decoder:
        with pytest.raises(InboundTransferError,match="INBOUND_CHUNK_SIZE_LIMIT"):
            store.put_chunk("UTESTTOKEN1",index=0,crc32="00000000",
                            token="A"*(max_token_chars+1))
    decoder.assert_not_called()


def test_concurrent_inbound_atomic_writers_use_distinct_staging_files(tmp_path):
    path=tmp_path/"inbound"/"USAMEINDEX"/"0000.part"
    both=[b"FIRST FRAME",b"SECOND FRAME"]
    barrier=threading.Barrier(2)
    lock=threading.Lock()
    temp_paths=[]
    mkstemp=inbound_module.tempfile.mkstemp

    def simultaneous(*args,**kwargs):
        descriptor,name=mkstemp(*args,**kwargs)
        with lock:
            temp_paths.append(name)
        barrier.wait(timeout=10)
        return descriptor,name

    with patch.object(inbound_module.tempfile,"mkstemp",
                      side_effect=simultaneous):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda raw:InboundCommandStore._atomic_write(path,raw),both))
    assert path.read_bytes() in both
    assert len(set(temp_paths))==2
    assert list(path.parent.glob(".*.tmp"))==[]


def test_failed_chunk_atomic_replace_preserves_previous_bytes_and_cleans_temp(tmp_path):
    path=tmp_path/"inbound"/"USAMEINDEX"/"0000.part"
    InboundCommandStore._atomic_write(path,b"original")
    with patch.object(inbound_module.os,"replace",
                      side_effect=OSError("DISK_UNAVAILABLE")):
        with pytest.raises(OSError,match="DISK_UNAVAILABLE"):
            InboundCommandStore._atomic_write(path,b"replacement")
    assert path.read_bytes()==b"original"
    assert list(path.parent.glob(".*.tmp"))==[]


@pytest.mark.parametrize("field,invalid",(
    ("chunk_count",10**12),
    ("chunk_count",True),
    ("raw_bytes",10**12),
    ("compressed_bytes",-1),
    ("version",2),
    ("transfer_id","UOTHER999"),
    ("codec","untrusted"),
    ("sha256","invalid"),
))
def test_corrupt_persisted_metadata_cannot_start_unbounded_chunk_scan(
        tmp_path,monkeypatch,field,invalid):
    store=InboundCommandStore(tmp_path/"inbound")
    transfer_id="UMETACHECK01"
    raw=b"valid command"
    compressed=inbound_module.zlib.compress(raw)
    store.begin(transfer_id,raw_bytes=len(raw),
                compressed_bytes=len(compressed),chunk_count=1,
                sha256=hashlib.sha256(raw).hexdigest())
    path=store._meta_path(transfer_id)
    metadata=json.loads(path.read_text(encoding="utf-8"))
    metadata[field]=invalid
    raw_meta=json.dumps(metadata).encode("utf-8")
    path.write_bytes(raw_meta)
    monkeypatch.setattr(store,"_missing_locked",
                        lambda *_: (_ for _ in ()).throw(
                            AssertionError("untrusted count iterated")))
    with pytest.raises(InboundTransferError,match="INBOUND_TRANSFER_META_CORRUPT"):
        store.status(transfer_id)
    assert path.read_bytes()==raw_meta


def test_large_persisted_transfer_metadata_rejected_without_decoding(tmp_path,monkeypatch):
    store=InboundCommandStore(tmp_path/"inbound")
    transfer_id="ULARGEMETA01"
    path=store._meta_path(transfer_id)
    path.parent.mkdir(parents=True)
    payload=b'{"junk":"' + b"x"*20000 + b'"}'
    path.write_bytes(payload)
    with patch.object(inbound_module.json,"loads",
                      side_effect=AssertionError("oversized meta parsed")) as decoder:
        with pytest.raises(InboundTransferError,match="INBOUND_TRANSFER_META_CORRUPT"):
            store.status(transfer_id)
    decoder.assert_not_called()
    assert path.read_bytes()==payload
