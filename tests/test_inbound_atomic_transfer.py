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


@pytest.mark.parametrize("invalid", (True, False, 1.5, float("nan"), float("inf"),
                                     None, "3.5", "9"*500))
def test_inbound_numeric_fields_reject_invalid_types_with_protocol_code(tmp_path, invalid):
    store=InboundCommandStore(tmp_path / "inbound")
    meta={"raw_bytes":10,"compressed_bytes":8,
          "chunk_count":1,"sha256":"a"*64}
    for name,code in (
            ("raw_bytes","INBOUND_RAW_SIZE_LIMIT"),
            ("compressed_bytes","INBOUND_COMPRESSED_SIZE_LIMIT"),
            ("chunk_count","INBOUND_CHUNK_COUNT_LIMIT")):
        with pytest.raises(InboundTransferError) as exc:
            store.begin("UNUMERIC1",**{**meta,name:invalid})
        assert exc.value.code==code
    with pytest.raises(InboundTransferError) as exc:
        store.put_chunk("UNUMERIC1",index=invalid,
                        crc32="00000000",token="AAAA")
    assert exc.value.code=="INBOUND_CHUNK_INDEX_OUT_OF_RANGE"


def test_inbound_numeric_fields_preserve_decimal_string_transport(tmp_path):
    store=InboundCommandStore(tmp_path / "inbound")
    receipt=store.begin("UNUMERIC2",raw_bytes="10",
                        compressed_bytes="8",chunk_count="1",
                        sha256="a"*64)
    assert receipt["raw_bytes"]==10
    assert receipt["compressed_bytes"]==8
    assert receipt["chunk_count"]==1
    # Valid decimal-string indexes reach CRC validation normally.
    with pytest.raises(InboundTransferError) as exc:
        store.put_chunk("UNUMERIC2",index="0",
                        crc32="00000000",token="AQ")
    assert exc.value.code=="INBOUND_CHUNK_CRC_MISMATCH"


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


def test_distinct_store_instances_cannot_replace_conflicting_chunk_concurrently(tmp_path):
    import base64
    import binascii
    from concurrent.futures import TimeoutError as FutureTimeout

    root=tmp_path/"inbound"
    first=InboundCommandStore(root)
    second=InboundCommandStore(root)
    first.begin("UCROSSLOCK1",raw_bytes=2,compressed_bytes=2,
                chunk_count=1,sha256="a"*64)
    one,two=b"AB",b"CD"
    def args(payload):
        return {"index":0,"crc32":f"{binascii.crc32(payload)&0xffffffff:08x}",
                "token":base64.urlsafe_b64encode(payload).decode().rstrip("=")}

    entered=threading.Event()
    release=threading.Event()
    second_started=threading.Event()
    original=InboundCommandStore._atomic_write

    def hold_first(path,payload):
        if path.name=="0000.part" and payload==one:
            entered.set()
            assert release.wait(timeout=5)
        return original(path,payload)

    def second_request():
        second_started.set()
        return second.put_chunk("UCROSSLOCK1",**args(two))

    with patch.object(InboundCommandStore,"_atomic_write",
                      staticmethod(hold_first)):
        with ThreadPoolExecutor(max_workers=2) as pool:
            primary=pool.submit(first.put_chunk,"UCROSSLOCK1",**args(one))
            assert entered.wait(timeout=3)
            competitor=pool.submit(second_request)
            try:
                assert second_started.wait(timeout=3)
                # The second writer must wait for the first transaction's
                # commit, not overwrite the same missing .part concurrently.
                with pytest.raises(FutureTimeout):
                    competitor.result(timeout=.12)
            finally:
                release.set()
            assert primary.result(timeout=5)["duplicate"] is False
            with pytest.raises(InboundTransferError) as exc:
                competitor.result(timeout=5)
            assert exc.value.code=="INBOUND_CHUNK_CONFLICT"
    assert (root/"UCROSSLOCK1"/"0000.part").read_bytes()==one


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


def test_oversized_persisted_part_is_read_with_strict_size_limit(tmp_path, monkeypatch):
    store=InboundCommandStore(tmp_path/"inbound")
    raw=b":state"
    compressed=inbound_module.zlib.compress(raw)
    transfer_id="UPARTSIZE01"
    store.begin(transfer_id,raw_bytes=len(raw),compressed_bytes=len(compressed),
                chunk_count=1,sha256=hashlib.sha256(raw).hexdigest())
    target=store._dir(transfer_id)/"0000.part"
    # This file is not an incoming request; it represents a legacy or
    # externally corrupted spool restored after a process restart.
    oversized=b"X"*(store.MAX_CHUNK_BYTES+100)
    target.write_bytes(oversized)
    with pytest.raises(InboundTransferError,match="INBOUND_CHUNK_SIZE_LIMIT"):
        store.status(transfer_id)
    with pytest.raises(InboundTransferError,match="INBOUND_CHUNK_SIZE_LIMIT"):
        store.assemble(transfer_id)
    token=inbound_module.base64.urlsafe_b64encode(compressed).decode().rstrip("=")
    crc=f"{inbound_module.binascii.crc32(compressed)&0xffffffff:08x}"
    with pytest.raises(InboundTransferError,match="INBOUND_CHUNK_SIZE_LIMIT"):
        store.put_chunk(transfer_id,index=0,crc32=crc,token=token)
    # Failed reads do not destroy forensic evidence or prevent repair.
    assert target.read_bytes()==oversized
    target.write_bytes(compressed)
    assert store.status(transfer_id)["ready"] is True
    assert store.assemble(transfer_id)[0]==raw.decode()


def test_oversized_persisted_part_rejected_before_loading_whole_file(tmp_path, monkeypatch):
    store=InboundCommandStore(tmp_path/"inbound")
    raw=b":state"
    compressed=inbound_module.zlib.compress(raw)
    transfer_id="UBOUNDEDREAD1"
    store.begin(transfer_id,raw_bytes=len(raw),compressed_bytes=len(compressed),
                chunk_count=1,sha256=hashlib.sha256(raw).hexdigest())
    target=store._dir(transfer_id)/"0000.part"
    target.write_bytes(b"X"*(store.MAX_CHUNK_BYTES+10))
    original_open=type(target).open
    requests=[]
    class Observed:
        def __init__(self,stream):self._stream=stream
        def __enter__(self):return self
        def __exit__(self,*a):self._stream.close()
        def read(self,length=-1):
            requests.append(length)
            return self._stream.read(length)
    def monitored_open(path,mode="r",*args,**kwargs):
        stream=original_open(path,mode,*args,**kwargs)
        if path==target and mode=="rb":
            return Observed(stream)
        return stream
    monkeypatch.setattr(type(target),"open",monitored_open)
    with pytest.raises(InboundTransferError,match="INBOUND_CHUNK_SIZE_LIMIT"):
        store.assemble(transfer_id)
    assert requests==[store.MAX_CHUNK_BYTES+1]
