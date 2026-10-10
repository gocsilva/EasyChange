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
