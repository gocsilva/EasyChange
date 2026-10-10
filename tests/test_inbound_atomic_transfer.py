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


@pytest.mark.parametrize("identifier", (
    "Uß234567", "Uı234567", "U１２３４５６７", "U١٢٣٤٥٦٧",
    "U😀234567", "U/path123", "U.dot123", "U" * 65,
    "", None, 123,
))
def test_inbound_transfer_id_rejects_unicode_aliases_before_staging(tmp_path, identifier):
    store=InboundCommandStore(tmp_path / "inbound")
    with pytest.raises(InboundTransferError) as exc:
        store.begin(identifier,raw_bytes=10,compressed_bytes=8,
                    chunk_count=1,sha256="a"*64)
    assert exc.value.code=="INVALID_INBOUND_TRANSFER_ID"
    assert not any(item.is_dir() for item in store.root.iterdir())


def test_inbound_lock_context_is_invoked_for_full_transfer_lifecycle(tmp_path):
    import base64
    import binascii
    import zlib

    store=InboundCommandStore(tmp_path / "transfer")
    command=b":state"
    encoded=zlib.compress(command)
    key="ULOCKFLOW01"
    digest=hashlib.sha256(command).hexdigest()
    begin=store.begin(key,raw_bytes=len(command),
                      compressed_bytes=len(encoded),chunk_count=1,
                      sha256=digest)
    assert begin["duplicate"] is False
    assert store.status(key)["ready"] is False
    token=base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
    crc=f"{binascii.crc32(encoded) & 0xffffffff:08x}"
    assert store.put_chunk(key,index=0,crc32=crc,token=token)["duplicate"] is False
    assert store.status(key)["verified"] is True
    recovered,receipt=store.assemble(key)
    assert recovered==":state" and receipt["sha256"]==digest
    assert store.abort(key)["removed"] is True
    assert store.abort(key)["removed"] is False


def test_inbound_lock_rejects_symlink_without_touching_external_file(tmp_path):
    store=InboundCommandStore(tmp_path / "transfer")
    foreign=tmp_path / "private.txt"
    foreign.write_bytes(b"do not write to this file")
    try:
        store._process_lock_path.symlink_to(foreign)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("Symlinks not available: "+type(exc).__name__)
    with pytest.raises(InboundTransferError,match="INBOUND_UNSAFE_LOCK_PATH"):
        store.begin("USAFEPATH1",raw_bytes=3,compressed_bytes=3,
                    chunk_count=1,sha256="a"*64)
    assert foreign.read_bytes()==b"do not write to this file"
    assert store._process_lock_path.is_symlink()
    store._process_lock_path.unlink()
    assert store.begin("USAFEPATH1",raw_bytes=3,compressed_bytes=3,
                       chunk_count=1,sha256="a"*64)["duplicate"] is False


def test_inbound_lock_fdopen_failure_releases_raw_descriptor(tmp_path):
    import os
    store=InboundCommandStore(tmp_path / "transfer")
    original_close=os.close
    closed=[]
    def track_close(fd):
        closed.append(fd)
        return original_close(fd)
    with patch.object(inbound_module.os,"fdopen",
                      side_effect=OSError("LOCK_STREAM_FAILURE")), \
         patch.object(inbound_module.os,"close",side_effect=track_close):
        with pytest.raises(OSError,match="LOCK_STREAM_FAILURE"):
            with store._transaction_lock():
                raise AssertionError("Lock was unexpectedly acquired")
    assert len(closed)==1
    # A failed wrapper must not strand the file or the next live lock.
    with store._transaction_lock():
        assert store._process_lock_path.is_file()


def test_inbound_lock_rejects_hardlink_to_another_regular_file(tmp_path):
    import os

    store=InboundCommandStore(tmp_path / "transfer")
    foreign=tmp_path / "external-control.txt"
    foreign.write_bytes(b"private contents")
    try:
        os.link(foreign,store._process_lock_path)
    except (OSError,NotImplementedError) as exc:
        pytest.skip("Hard links not supported: "+type(exc).__name__)
    with pytest.raises(InboundTransferError,match="INBOUND_UNSAFE_LOCK_TYPE"):
        store.begin("UHARDLINK1",raw_bytes=3,compressed_bytes=3,
                    chunk_count=1,sha256="a"*64)
    assert foreign.read_bytes()==b"private contents"
    store._process_lock_path.unlink()
    assert store.begin("UHARDLINK1",raw_bytes=3,compressed_bytes=3,
                       chunk_count=1,sha256="a"*64)["duplicate"] is False


def test_symlinked_staging_chunk_never_reads_foreign_file(tmp_path):
    store=InboundCommandStore(tmp_path / "inbound")
    foreign=tmp_path / "private.txt"
    foreign.write_bytes(b"private content never an inbound chunk")
    target=tmp_path / "staged.part"
    try:
        target.symlink_to(foreign)
    except (OSError,NotImplementedError) as exc:
        pytest.skip("Symlink privilege unavailable: "+type(exc).__name__)
    with pytest.raises(InboundTransferError,match="INBOUND_CHUNK_READ_FAILED"):
        store._read_chunk_bounded(target)
    assert foreign.read_bytes()==b"private content never an inbound chunk"


def test_special_fifo_chunk_rejected_without_blocking_reader(tmp_path):
    import os
    if not (hasattr(os,"mkfifo") and hasattr(os,"O_NONBLOCK")):
        pytest.skip("Platform lacks a nonblocking FIFO")
    target=tmp_path / "named-pipe.part"
    os.mkfifo(target)
    with pytest.raises(InboundTransferError,match="INBOUND_CHUNK_READ_FAILED"):
        InboundCommandStore._read_chunk_bounded(target)


def test_symlinked_transfer_directory_cannot_redirect_staging_or_abort(tmp_path):
    store=InboundCommandStore(tmp_path / "inbound")
    external=tmp_path / "outside-project"
    external.mkdir()
    (external / "do-not-touch.txt").write_text("private",encoding="utf-8")
    alias=store.root / "UOUTSIDE01"
    try:
        alias.symlink_to(external,target_is_directory=True)
    except (OSError,NotImplementedError) as exc:
        pytest.skip("Directory symlinks unavailable: "+type(exc).__name__)
    with pytest.raises(InboundTransferError,match="INBOUND_UNSAFE_TRANSFER_DIRECTORY"):
        store.begin("UOUTSIDE01",raw_bytes=3,compressed_bytes=3,
                    chunk_count=1,sha256="a"*64)
    with pytest.raises(InboundTransferError,match="INBOUND_UNSAFE_TRANSFER_DIRECTORY"):
        store.status("UOUTSIDE01")
    with pytest.raises(InboundTransferError,match="INBOUND_UNSAFE_TRANSFER_DIRECTORY"):
        store.abort("UOUTSIDE01")
    assert (external / "do-not-touch.txt").read_text(encoding="utf-8")=="private"
    assert alias.is_symlink()
    alias.unlink()
    assert store.begin("UOUTSIDE01",raw_bytes=3,compressed_bytes=3,
                       chunk_count=1,sha256="a"*64)["duplicate"] is False


def test_inbound_transfer_ascii_casefold_preserves_existing_directory(tmp_path):
    store=InboundCommandStore(tmp_path / "inbound")
    first=store.begin("uss234567",raw_bytes=10,compressed_bytes=8,
                      chunk_count=1,sha256="a"*64)
    assert first["transfer_id"]=="USS234567"
    with pytest.raises(InboundTransferError) as exc:
        store.begin("Uß234567",raw_bytes=10,compressed_bytes=8,
                    chunk_count=1,sha256="b"*64)
    assert exc.value.code=="INVALID_INBOUND_TRANSFER_ID"
    second=store.begin("USS234567",raw_bytes=10,compressed_bytes=8,
                       chunk_count=1,sha256="a"*64)
    assert second["duplicate"] is True
    assert len([p for p in store.root.iterdir() if p.is_dir()])==1


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


def test_failed_stream_wrapper_releases_chunk_fd_and_removes_staging(tmp_path):
    import os
    target=tmp_path / "part-0.bin"
    real_close=os.close
    closed=[]
    def tracked_close(fd):
        closed.append(fd)
        return real_close(fd)
    with patch.object(inbound_module.os,"fdopen",
                      side_effect=OSError("SIMULATED_FDOPEN_ERROR")), \
         patch.object(inbound_module.os,"close",side_effect=tracked_close):
        with pytest.raises(OSError,match="SIMULATED_FDOPEN_ERROR"):
            InboundCommandStore._atomic_write(target,b"chunk")
    assert len(closed)==1
    assert not target.exists()
    assert list(tmp_path.iterdir())==[]


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
