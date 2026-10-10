"""Human-text buffer metadata must reflect the actual atomic file version."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
from unittest.mock import patch

import pytest

import easychange.core.human_text_service as human_module
from easychange.core.human_text_service import HumanTextService


def test_same_size_external_file_replacement_invalidates_cached_metadata(tmp_path):
    service=HumanTextService(tmp_path)
    original=service.save("AAAA")
    assert original["bytes"]==4
    service._atomic_write(service.path,b"BBBB")
    refreshed=service.info()
    assert refreshed["bytes"]==4
    assert refreshed["sha256"]==hashlib.sha256(b"BBBB").hexdigest()
    assert refreshed["file_inode"]==service.path.stat().st_ino
    assert refreshed["file_mtime_ns"]==service.path.stat().st_mtime_ns


def test_simultaneous_writers_use_unique_staging_paths(tmp_path):
    service=HumanTextService(tmp_path)
    first=b"first value"
    second=b"other value"
    barrier=threading.Barrier(2)
    guard=threading.Lock()
    names=[]
    real_mkstemp=human_module.tempfile.mkstemp

    def simultaneously(*args,**kwargs):
        fd,name=real_mkstemp(*args,**kwargs)
        with guard:
            names.append(name)
        barrier.wait(timeout=10)
        return fd,name

    with patch.object(human_module.tempfile,"mkstemp",
                      side_effect=simultaneously):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(
                lambda payload:service._atomic_write(service.path,payload),
                (first,second)))
    assert service.path.read_bytes() in (first,second)
    assert len(set(names))==2
    assert not list(service.root.glob(".*.tmp"))


def test_failed_text_replace_preserves_existing_buffer(tmp_path):
    service=HumanTextService(tmp_path)
    service.save("old content")
    with patch.object(human_module.os,"replace",
                      side_effect=OSError("WRITE_FAILED")):
        with pytest.raises(OSError,match="WRITE_FAILED"):
            service.save("new content")
    assert service.load()=="old content"
    assert not list(service.root.glob(".*.tmp"))


def test_human_text_stream_wrapper_error_releases_fd_and_temp(tmp_path):
    import os

    service=HumanTextService(tmp_path)
    previous=service.save("before")
    original_close=os.close
    closed=[]
    def tracked_close(fd):
        closed.append(fd)
        return original_close(fd)
    with patch.object(human_module.os,"fdopen",
                      side_effect=OSError("STREAM_WRAPPER_FAILED")), \
         patch.object(human_module.os,"close",side_effect=tracked_close):
        with pytest.raises(OSError,match="STREAM_WRAPPER_FAILED"):
            service.save("after")
    assert len(closed)==1
    assert service.load()=="before"
    assert service.info()["sha256"]==previous["sha256"]
    assert not list(service.root.glob(".*.tmp"))


def test_paginated_read_rejects_stale_metadata_after_same_size_replacement(tmp_path):
    service=HumanTextService(tmp_path)
    service.save("AAAA")
    previous=service.info()
    service._atomic_write(service.path,b"BBBB")
    with patch.object(service,"info",return_value=previous):
        with pytest.raises(ValueError,match="HUMAN_TEXT_FILE_CHANGED_RETRY"):
            service.read(cursor=0,max_bytes=512)
    with patch.object(service,"info",return_value=previous):
        with pytest.raises(ValueError,match="HUMAN_TEXT_FILE_CHANGED_RETRY"):
            service.read(cursor=4,max_bytes=512)
    assert service.read(cursor=0)["text"]=="BBBB"


def test_paginated_read_validates_utf8_boundary_on_open_file(tmp_path):
    service=HumanTextService(tmp_path)
    service.save("ábc")
    with pytest.raises(ValueError,match="HUMAN_TEXT_CURSOR_NOT_UTF8_BOUNDARY"):
        service.read(cursor=1)
    page=service.read(cursor=2)
    assert page["text"]=="bc"
    assert page["complete"] is True


def test_scan_rejects_atomic_replacement_during_hashing(tmp_path):
    from pathlib import Path

    service=HumanTextService(tmp_path)
    service.save("AAAA")
    original_stat=Path.stat
    checks=[]
    def concurrent_swap(self,*args,**kwargs):
        if self==service.path:
            checks.append(1)
            if len(checks)==2:
                service._atomic_write(service.path,b"BBBB")
        return original_stat(self,*args,**kwargs)
    with patch.object(Path,"stat",concurrent_swap):
        with pytest.raises(ValueError,match="HUMAN_TEXT_FILE_CHANGED_RETRY"):
            service._scan_info()
    assert service.load()=="BBBB"
    assert service.info()["sha256"]==hashlib.sha256(b"BBBB").hexdigest()


def test_scan_refuses_large_legacy_buffer_before_hashing(tmp_path):
    service=HumanTextService(tmp_path)
    service.path.write_bytes(b"X"*256)
    service.max_bytes=64
    with pytest.raises(ValueError,match="HUMAN_TEXT_EXCEEDS_CONFIGURED_LIMIT"):
        service._scan_info()


def test_human_text_load_rejects_overlong_data_with_bounded_read(tmp_path):
    service=HumanTextService(tmp_path)
    service.path.write_bytes(b"A"*200)
    # Lower the per-instance budget in this isolated test.
    service.max_bytes=16
    with pytest.raises(ValueError,match="HUMAN_TEXT_EXCEEDS_CONFIGURED_LIMIT"):
        service.load()
    assert service.path.read_bytes()==b"A"*200


def test_human_text_load_rejects_invalid_utf8_without_rewriting_file(tmp_path):
    service=HumanTextService(tmp_path)
    service.path.write_bytes(b"invalid\xffbytes")
    with pytest.raises(ValueError,match="HUMAN_TEXT_INVALID_UTF8"):
        service.load()
    assert service.path.read_bytes()==b"invalid\xffbytes"


def test_empty_buffer_metadata_is_cached_without_full_rescan(tmp_path, monkeypatch):
    service=HumanTextService(tmp_path)
    saved=service.save("")
    assert saved["bytes"]==0
    assert service.path.is_file()
    with monkeypatch.context() as ctx:
        ctx.setattr(service, "_scan_info",
                    lambda: (_ for _ in ()).throw(
                        AssertionError("empty buffer should use metadata cache")))
        cached=service.info()
    assert cached["bytes"]==0
    assert cached["sha256"]==hashlib.sha256(b"").hexdigest()


def test_oversized_metadata_falls_back_to_verified_text_without_unbounded_read(tmp_path,monkeypatch):
    from pathlib import Path

    service=HumanTextService(tmp_path)
    saved=service.save("persisted content")
    oversized=b'{"bytes":17,"padding":"' + b"x"*2048 + b'"}'
    service.meta_path.write_bytes(oversized)
    observed=[]
    original_open=Path.open

    class MeasuredReader:
        def __init__(self,inner):
            self.inner=inner
        def __enter__(self):
            return self
        def __exit__(self,*args):
            return self.inner.__exit__(*args)
        def read(self,size=-1):
            observed.append(size)
            return self.inner.read(size)

    def guarded_open(path,*args,**kwargs):
        handle=original_open(path,*args,**kwargs)
        if path==service.meta_path and args and args[0]=="rb":
            return MeasuredReader(handle)
        return handle

    with monkeypatch.context() as ctx:
        ctx.setattr(human_module,"MAX_HUMAN_TEXT_METADATA_BYTES",128)
        ctx.setattr(Path,"open",guarded_open)
        refreshed=service.info()
    assert observed==[129]
    assert refreshed["sha256"]==saved["sha256"]
    assert service.load()=="persisted content"
    assert service.meta_path.read_bytes()!=oversized


def test_malformed_metadata_digest_is_not_trusted_as_buffer_identity(tmp_path):
    import json

    service=HumanTextService(tmp_path)
    saved=service.save("safe data")
    spoof=dict(saved)
    spoof["sha256"]="WRONG"
    service.meta_path.write_text(json.dumps(spoof),encoding="utf-8")
    verified=service.info()
    assert verified["sha256"]==hashlib.sha256(b"safe data").hexdigest()
    assert verified["sha256"]!="WRONG"


def test_human_text_save_rejects_impossible_character_count_before_utf8_copy(tmp_path):
    service=HumanTextService(tmp_path)
    service.max_bytes=16
    with pytest.raises(ValueError,match="HUMAN_TEXT_TOO_LARGE"):
        service.save("x"*10000)
    assert not service.path.exists()
    assert service.save("ótimo")["bytes"]==len("ótimo".encode("utf-8"))


@pytest.mark.parametrize(("value","code"), [
    (True,"HUMAN_TEXT_INVALID_CURSOR"),(False,"HUMAN_TEXT_INVALID_CURSOR"),
    (1.5,"HUMAN_TEXT_INVALID_CURSOR"),
    (float("nan"),"HUMAN_TEXT_INVALID_CURSOR"),
    (float("inf"),"HUMAN_TEXT_INVALID_CURSOR"),
    (10**300,"HUMAN_TEXT_INVALID_CURSOR"),
    ("1.0000000000000001","HUMAN_TEXT_INVALID_CURSOR"),
])
def test_human_text_invalid_cursors_are_rejected_before_disk_scan(tmp_path,monkeypatch,value,code):
    service=HumanTextService(tmp_path)
    service.save("safe")
    monkeypatch.setattr(service,"info",
        lambda: (_ for _ in ()).throw(AssertionError("INVALID_CURSOR_TRIGGERED_DISK_IO")))
    with pytest.raises(ValueError,match=code):
        service.read(cursor=value)


@pytest.mark.parametrize("value", [
    True, False, float("nan"), float("inf"), -float("inf"),
    12.5, 10**300, "1.0000000000000001", [], {},
])
def test_human_text_invalid_page_size_is_rejected_before_io(tmp_path,monkeypatch,value):
    service=HumanTextService(tmp_path)
    monkeypatch.setattr(service,"info",
        lambda: (_ for _ in ()).throw(AssertionError("INVALID_PAGE_SIZE_TRIGGERED_DISK_IO")))
    with pytest.raises(ValueError,match="HUMAN_TEXT_INVALID_PAGE_SIZE"):
        service.read(max_bytes=value)


def test_human_text_page_options_preserve_exact_large_cursor_contract(tmp_path):
    service=HumanTextService(tmp_path)
    service.save("abc")
    assert service.read(cursor="1",max_bytes="512")["text"]=="bc"
    assert service.read(cursor=0,max_bytes=0)["text"]=="abc"
    with pytest.raises(ValueError,match="HUMAN_TEXT_CURSOR_OUT_OF_RANGE"):
        service.read(cursor=(1<<53)+1)
