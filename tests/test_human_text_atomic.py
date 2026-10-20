"""Human-text buffer metadata must reflect the actual atomic file version."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import threading
from unittest.mock import patch

import pytest

import easychange.core.human_text_service as human_module
from easychange.core.human_text_service import HumanTextService


def test_cached_metadata_cannot_bypass_a_lowered_size_limit(tmp_path, monkeypatch):
    # A prior instance may have legitimately saved a larger buffer. When the
    # workspace limit is later lowered, a matching inode/mtime cache cannot
    # make that now-oversized buffer appear valid.
    monkeypatch.setenv("EASYCHANGE_MAX_HUMAN_TEXT_BYTES", "2097152")
    writer=HumanTextService(tmp_path)
    payload="x" * (1024 * 1024 + 19)
    saved=writer.save(payload)
    assert writer.info()["sha256"]==saved["sha256"]

    monkeypatch.setenv("EASYCHANGE_MAX_HUMAN_TEXT_BYTES", "1048576")
    reader=HumanTextService(tmp_path)
    assert reader.max_bytes==1024 * 1024
    with pytest.raises(ValueError, match="HUMAN_TEXT_EXCEEDS_CONFIGURED_LIMIT"):
        reader.info()
    with pytest.raises(ValueError, match="HUMAN_TEXT_EXCEEDS_CONFIGURED_LIMIT"):
        reader.read(cursor=0, max_bytes=512)
    assert reader.path.read_bytes()==payload.encode("utf-8")

    # Raising the limit again restores access without rewriting the file.
    monkeypatch.setenv("EASYCHANGE_MAX_HUMAN_TEXT_BYTES", "2097152")
    restored=HumanTextService(tmp_path)
    assert restored.info()["sha256"]==saved["sha256"]


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


@pytest.mark.skipif(os.name != "nt", reason="Win32 replace sharing violations")
def test_atomic_write_retries_transient_windows_replace_failure(tmp_path):
    target=tmp_path/"buffer.txt"
    real_replace=human_module.os.replace
    attempts=[]

    def transient_once(source,destination):
        attempts.append(1)
        if len(attempts)==1:
            error=PermissionError("temporary sharing violation")
            error.winerror=5
            raise error
        return real_replace(source,destination)

    with patch.object(human_module.os,"replace",side_effect=transient_once):
        HumanTextService._atomic_write(target,b"atomic payload")

    assert len(attempts)==2
    assert target.read_bytes()==b"atomic payload"
    assert not list(tmp_path.glob(".*.tmp"))


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
    from types import SimpleNamespace

    service=HumanTextService(tmp_path)
    service.save("AAAA")
    original_lstat=Path.lstat

    def replaced_generation(path,*args,**kwargs):
        current=original_lstat(path,*args,**kwargs)
        if path==service.path:
            return SimpleNamespace(
                st_dev=current.st_dev,
                st_ino=current.st_ino+1,
                st_size=current.st_size,
                st_mtime_ns=current.st_mtime_ns,
                st_mode=current.st_mode,
                st_nlink=current.st_nlink,
            )
        return current

    with patch.object(Path,"lstat",replaced_generation):
        with pytest.raises(ValueError,match="HUMAN_TEXT_FILE_CHANGED_RETRY"):
            service._scan_info()
    service._atomic_write(service.path,b"BBBB")
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
    service=HumanTextService(tmp_path)
    saved=service.save("persisted content")
    oversized=b'{"bytes":17,"padding":"' + b"x"*2048 + b'"}'
    service.meta_path.write_bytes(oversized)
    observed=[]
    original_read=human_module.os.read

    def guarded_read(fd,size):
        observed.append(size)
        return original_read(fd,size)

    with monkeypatch.context() as ctx:
        ctx.setattr(human_module,"MAX_HUMAN_TEXT_METADATA_BYTES",128)
        ctx.setattr(human_module.os,"read",guarded_read)
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


def test_save_rejects_unicode_byte_expansion_before_atomic_replace(tmp_path):
    service=HumanTextService(tmp_path)
    original=service.save("keep existing")
    service.max_bytes=14
    # Four code points fit in the character limit, but their UTF-8 encoding
    # requires 16 bytes. The rejection must not touch an existing buffer.
    with pytest.raises(ValueError, match="HUMAN_TEXT_TOO_LARGE"):
        service.save("🚀"*4)
    assert service.path.read_text(encoding="utf-8")=="keep existing"
    assert service.meta_path.exists()
    assert service.info()["sha256"]==original["sha256"]

    valid="🚀"*3
    saved=service.save(valid)
    assert saved["bytes"]==12
    assert service.load()==valid


def test_save_invalid_utf8_surrogate_is_deterministic_input_error(tmp_path):
    service=HumanTextService(tmp_path)
    with pytest.raises(ValueError, match="HUMAN_TEXT_INVALID_UTF8"):
        service.save("invalid surrogate: \ud800")
    assert not service.path.exists()


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


def test_human_text_symlinked_metadata_cannot_read_or_mutate_foreign_file(tmp_path):
    service=HumanTextService(tmp_path)
    saved=service.save("actual content")
    outside=tmp_path/"foreign-record.json"
    outside.write_text('{"secret":"unrelated"}',encoding="utf-8")
    service.meta_path.unlink()
    try:
        service.meta_path.symlink_to(outside)
    except (OSError,NotImplementedError) as exc:
        pytest.skip("Symlinks unavailable: "+type(exc).__name__)
    recovered=service.info()
    assert recovered["sha256"]==saved["sha256"]
    assert service.meta_path.is_file()
    assert not service.meta_path.is_symlink()
    assert outside.read_text(encoding="utf-8")=='{"secret":"unrelated"}'


@pytest.mark.skipif(__import__("os").name!="posix",reason="POSIX FIFO nonblocking safety")
def test_human_text_fifo_metadata_does_not_block_recovery(tmp_path):
    import os

    service=HumanTextService(tmp_path)
    saved=service.save("plain text")
    service.meta_path.unlink()
    os.mkfifo(service.meta_path)
    recovered=service.info()
    assert recovered["sha256"]==saved["sha256"]
    assert service.meta_path.is_file()

def test_human_text_buffer_symlink_never_reads_foreign_text(tmp_path):
    import os

    service=HumanTextService(tmp_path/"workspace")
    foreign=tmp_path/"external-private.txt"
    foreign.write_text("FOREIGN SECRET",encoding="utf-8")
    try:
        service.path.symlink_to(foreign)
    except (OSError,NotImplementedError):
        pytest.skip("symlink creation unavailable on this host")
    with pytest.raises(ValueError,match="HUMAN_TEXT_UNSAFE_BUFFER"):
        service.load()
    with pytest.raises(ValueError,match="HUMAN_TEXT_UNSAFE_BUFFER"):
        service.info()
    with pytest.raises(ValueError,match="HUMAN_TEXT_UNSAFE_BUFFER"):
        service.read(cursor=0)
    assert foreign.read_text(encoding="utf-8")=="FOREIGN SECRET"


def test_human_text_fifo_buffer_is_rejected_without_blocking(tmp_path):
    import os

    if not hasattr(os,"mkfifo") or not hasattr(os,"O_NONBLOCK"):
        pytest.skip("FIFO/nonblocking open unsupported")
    service=HumanTextService(tmp_path)
    os.mkfifo(service.path)
    with pytest.raises(ValueError,match="HUMAN_TEXT_UNSAFE_BUFFER"):
        service.load()
    with pytest.raises(ValueError,match="HUMAN_TEXT_UNSAFE_BUFFER"):
        service.info()


def test_human_text_buffer_reader_rejects_hardlinks(tmp_path):
    import os

    service=HumanTextService(tmp_path/"workspace")
    foreign=tmp_path/"external-private.txt"
    foreign.write_text("confidential",encoding="utf-8")
    try:
        os.link(foreign,service.path)
    except OSError:
        pytest.skip("hard links unavailable")
    with pytest.raises(ValueError,match="HUMAN_TEXT_UNSAFE_BUFFER"):
        service.load()
    with pytest.raises(ValueError,match="HUMAN_TEXT_UNSAFE_BUFFER"):
        service.info()
    assert foreign.read_text(encoding="utf-8")=="confidential"


def test_human_text_regular_buffer_still_paginates_and_reloads(tmp_path):
    service=HumanTextService(tmp_path)
    payload="ábc 🚀 linha nova"
    service.save(payload)
    assert service.load()==payload
    assert service.info()["bytes"]==len(payload.encode("utf-8"))
    pages=[]
    cursor=0
    while True:
        page=service.read(cursor=cursor,max_bytes=512)
        pages.append(page["text"])
        if page["complete"]:
            break
        cursor=page["next_cursor"]
    assert "".join(pages)==payload



def test_human_text_matching_cached_metadata_never_trusts_symlink(tmp_path):
    service=HumanTextService(tmp_path/"workspace")
    original=service.save("private content")
    moved=tmp_path/"outside-buffer.txt"
    service.path.replace(moved)
    try:
        service.path.symlink_to(moved)
    except (OSError,NotImplementedError):
        pytest.skip("symlinks not available")
    assert moved.stat().st_ino==original["file_inode"]
    # The old Path.stat() fast path accepted this inode/size/mtime match.
    with pytest.raises(ValueError,match="HUMAN_TEXT_UNSAFE_BUFFER"):
        service.info()
    assert moved.read_text(encoding="utf-8")=="private content"


def test_human_text_regular_file_swap_during_safe_open_is_retryable(tmp_path,monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    service=HumanTextService(tmp_path)
    service.save("old content")
    actual_lstat=Path.lstat
    def replaced_generation(path,*args,**kwargs):
        current=actual_lstat(path,*args,**kwargs)
        if path==service.path:
            return SimpleNamespace(
                st_dev=current.st_dev,
                st_ino=current.st_ino+1,
                st_size=current.st_size,
                st_mtime_ns=current.st_mtime_ns,
                st_mode=current.st_mode,
                st_nlink=current.st_nlink,
            )
        return current
    monkeypatch.setattr(Path,"lstat",replaced_generation)
    with pytest.raises(ValueError,match="HUMAN_TEXT_FILE_CHANGED_RETRY"):
        service.load()
    monkeypatch.setattr(Path,"lstat",actual_lstat)
    service.save("new content")
    assert service.load()=="new content"




def test_dangling_buffer_symlink_is_not_mistaken_for_an_empty_buffer(tmp_path):
    service=HumanTextService(tmp_path)
    missing=tmp_path/"outside"/"not-present.txt"
    assert not missing.exists()
    try:
        service.path.symlink_to(missing)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert not service.path.exists()
    assert service.path.is_symlink()
    for operation in (service.info,service.load,
                      lambda: service.read(cursor=0)):
        with pytest.raises(ValueError,match="HUMAN_TEXT_UNSAFE_BUFFER"):
            operation()
    assert not missing.exists()


def test_racing_writer_cannot_publish_old_hash_with_new_file_identity(tmp_path,monkeypatch):
    writer_a=HumanTextService(tmp_path)
    writer_b=HumanTextService(tmp_path)
    real_atomic=writer_a._atomic_write
    completed=[]

    def interleaved_atomic(path,payload):
        committed=real_atomic(path,payload)
        if path==writer_a.path and not completed:
            completed.append(True)
            # Writer B commits between A's os.replace() and A's metadata.
            writer_b.save("BBBB")
        return committed

    monkeypatch.setattr(writer_a,"_atomic_write",interleaved_atomic)
    receipt_a=writer_a.save("AAAA")
    assert receipt_a["sha256"]==hashlib.sha256(b"AAAA").hexdigest()
    assert receipt_a["file_inode"]!=writer_a.path.stat().st_ino
    current=writer_a.info()
    assert writer_a.load()=="BBBB"
    assert current["sha256"]==hashlib.sha256(b"BBBB").hexdigest()
    assert current["file_inode"]==writer_a.path.stat().st_ino




def test_metadata_descriptor_must_match_path_inode_before_read(tmp_path, monkeypatch):
    from pathlib import Path

    service=HumanTextService(tmp_path/"workspace")
    saved=service.save("canonical buffer content")
    other=tmp_path/"other-metadata"
    other.write_text('{"sha256":"foreign"}',encoding="utf-8")
    actual_lstat=Path.lstat
    def swapped_identity(path,*args,**kwargs):
        if path==service.meta_path:
            # Simulate an adversarial inode swap immediately after os.open.
            return actual_lstat(other)
        return actual_lstat(path,*args,**kwargs)

    monkeypatch.setattr(Path,"lstat",swapped_identity)
    with pytest.raises(ValueError,match="HUMAN_TEXT_METADATA_FILE_CHANGED_RETRY"):
        service._read_metadata(service.meta_path)
    # Metadata is advisory. Its failure must not suppress access to the
    # canonical regular buffer, which is rescanned with a safe file handle.
    refreshed=service.info()
    assert refreshed["sha256"]==saved["sha256"]
    assert refreshed["bytes"]==len("canonical buffer content".encode("utf-8"))


def test_paginated_read_rejects_in_place_rewrite_during_page_io(tmp_path,monkeypatch):
    import os
    import easychange.core.human_text_service as module

    service=HumanTextService(tmp_path)
    service.save("AAAA")
    previous=service.info()
    original_fstat=module.os.fstat
    seen=[]

    def rewrite_on_final_fstat(fd):
        seen.append(fd)
        if len(seen)==3:
            # The descriptor already passed the initial signature check.
            # Another process mutates the same inode before completion.
            with service.path.open("r+b") as output:
                output.write(b"BBBB")
                output.flush()
                os.fsync(output.fileno())
            original=os.stat(service.path)
            os.utime(service.path,ns=(
                original.st_atime_ns,previous["file_mtime_ns"]+1_000_000_000))
        return original_fstat(fd)

    with monkeypatch.context() as ctx:
        ctx.setattr(service,"info",lambda:previous)
        ctx.setattr(module.os,"fstat",rewrite_on_final_fstat)
        with pytest.raises(ValueError,match="HUMAN_TEXT_FILE_CHANGED_RETRY"):
            service.read(cursor=0,max_bytes=512)
    assert seen
    assert service.load()=="BBBB"
    assert service.info()["sha256"]==hashlib.sha256(b"BBBB").hexdigest()




@pytest.mark.parametrize("read_operation",("read","scan"))
def test_buffer_symlink_swap_during_read_fails_even_if_target_has_same_inode(
        tmp_path,monkeypatch,read_operation):
    from contextlib import contextmanager

    service=HumanTextService(tmp_path/"workspace")
    service.save("canonical buffer")
    moved=tmp_path/"moved-original.txt"
    # Skip on hosts that cannot create symlinks without elevated privileges.
    probe=tmp_path/"symlink-probe"
    try:
        probe.symlink_to(service.path)
        probe.unlink()
    except (OSError,NotImplementedError):
        pytest.skip("symlinks unavailable")

    original_open=service._open_regular_buffer
    swapped=[False]

    @contextmanager
    def with_replacement_during_read(path):
        with original_open(path) as handle:
            class InterleavedRead:
                def fileno(self):
                    return handle.fileno()
                def tell(self):
                    return handle.tell()
                def seek(self,*args):
                    return handle.seek(*args)
                def read(self,*args):
                    value=handle.read(*args)
                    if not swapped[0]:
                        swapped[0]=True
                        service.path.replace(moved)
                        service.path.symlink_to(moved)
                    return value
            yield InterleavedRead()

    monkeypatch.setattr(service,"_open_regular_buffer",with_replacement_during_read)
    with pytest.raises(ValueError,match="HUMAN_TEXT_FILE_CHANGED_RETRY"):
        if read_operation=="scan":
            service._scan_info()
        else:
            service.read(cursor=0,max_bytes=512)
    assert swapped[0]
    assert service.path.is_symlink()
    assert moved.read_text(encoding="utf-8")=="canonical buffer"
