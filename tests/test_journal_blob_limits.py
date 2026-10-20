"""Journal snapshots must remain safe across corrupt data and concurrent writers."""
from __future__ import annotations

import concurrent.futures
import hashlib
import threading
import zlib
from unittest.mock import patch

import pytest

import easychange.core.journal as journal_module
from easychange.core.journal import Journal


def test_journal_blob_roundtrip_and_decompression_limit(tmp_path, monkeypatch):
    journal = Journal(tmp_path / ".easychange" / "journal.jsonl", "S1")
    contents = "sample-content\n" * 1200
    reference = journal._store_text(contents)
    assert reference.startswith("@zblob:")
    assert journal._load_text(reference) == contents

    monkeypatch.setattr(journal_module, "_MAX_BLOB_DECOMPRESSED_BYTES", 1024)
    with pytest.raises(ValueError, match="JOURNAL_BLOB_DECOMPRESSED_TOO_LARGE"):
        journal._load_text(reference)


def test_journal_snapshot_refuses_symlink_and_hardlink_alias(tmp_path):
    import os

    if os.name!="posix":
        pytest.skip("POSIX link validation only")
    journal=Journal(tmp_path/".easychange"/"journal.jsonl","S1")
    reference=journal._store_text("snapshot contents\\n"*900)
    target=journal.blob_root/(reference.removeprefix("@zblob:")+".zlib")
    backup=tmp_path/"original-compressed.zlib"
    original=target.read_bytes()
    target.unlink()
    backup.write_bytes(original)
    target.symlink_to(backup)
    with pytest.raises(ValueError,match="JOURNAL_BLOB_UNSAFE_FILE"):
        journal._load_text(reference)
    target.unlink()
    os.link(backup,target)
    with pytest.raises(ValueError,match="JOURNAL_BLOB_UNSAFE_FILE"):
        journal._load_text(reference)
    target.unlink()
    # The regular, single-linked content-addressed snapshot stays supported.
    target.write_bytes(original)
    assert journal._load_text(reference)=="snapshot contents\\n"*900


def test_fifo_in_journal_blob_folder_fails_without_blocking_open(tmp_path):
    import os

    if not hasattr(os,"mkfifo"):
        pytest.skip("FIFO unavailable")
    journal=Journal(tmp_path/".easychange"/"journal.jsonl","S1")
    reference=journal._store_text("large snapshot\\n"*900)
    target=journal.blob_root/(reference.removeprefix("@zblob:")+".zlib")
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(ValueError,match="JOURNAL_BLOB_UNSAFE_FILE"):
        journal._load_text(reference)


def test_journal_blob_compressed_limit_rejects_before_zlib_decode(tmp_path, monkeypatch):
    journal = Journal(tmp_path / ".easychange" / "journal.jsonl", "S1")
    content = "some original text" * 400
    reference = journal._store_text(content)
    monkeypatch.setattr(journal_module, "_MAX_BLOB_COMPRESSED_BYTES", 8)
    with patch.object(journal_module.zlib, "decompressobj",
                      side_effect=AssertionError("decompressed oversized input")) as decoder:
        with pytest.raises(ValueError, match="JOURNAL_BLOB_COMPRESSED_TOO_LARGE"):
            journal._load_text(reference)
    decoder.assert_not_called()


@pytest.mark.parametrize("damage", ("trailing", "truncated"))
def test_journal_blob_rejects_bad_zlib_stream_without_losing_snapshot(tmp_path, damage):
    journal = Journal(tmp_path / ".easychange" / "journal.jsonl", "S1")
    original = "snapshot contents\n" * 500
    reference = journal._store_text(original)
    path = journal.blob_root / (reference.removeprefix("@zblob:") + ".zlib")
    compressed = path.read_bytes()
    if damage == "trailing":
        path.write_bytes(compressed + b"UNEXPECTED_TRAILER")
    else:
        path.write_bytes(compressed[:-2])
    with pytest.raises(ValueError, match="JOURNAL_BLOB_INVALID_STREAM"):
        journal._load_text(reference)
    assert path.exists()


def test_simultaneous_identical_snapshot_writers_have_unique_staging_paths(tmp_path):
    journal = Journal(tmp_path / ".easychange" / "journal.jsonl", "S1")
    original = "\n".join(hashlib.sha256(str(i).encode()).hexdigest()
                         for i in range(150))
    ready = threading.Barrier(2)
    real_mkstemp = journal_module.tempfile.mkstemp
    temp_paths = []
    path_lock = threading.Lock()

    def concurrent_mkstemp(*args, **kwargs):
        handle, name = real_mkstemp(*args, **kwargs)
        with path_lock:
            temp_paths.append(name)
        ready.wait(timeout=10)
        return handle, name

    with patch.object(journal_module.tempfile, "mkstemp",
                      side_effect=concurrent_mkstemp):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            references = list(executor.map(journal._store_text, (original, original)))
    assert references == [references[0]] * 2
    assert len(set(temp_paths)) == 2
    assert journal._load_text(references[0]) == original
    assert not list(journal.blob_root.glob("*.tmp"))


@pytest.mark.parametrize("writer", ("blob", "rewrite"))
def test_fdopen_failure_releases_journal_staging_fd_and_preserves_file(tmp_path, writer):
    import os

    target=tmp_path / ".easychange" / "journal.jsonl"
    journal=Journal(target,"S1")
    if writer=="rewrite":
        journal.record("write","note.txt","before","after")
    previous=target.read_bytes() if target.exists() else None
    real_close=os.close
    closed=[]
    def tracked_close(fd):
        closed.append(fd)
        return real_close(fd)

    with patch.object(journal_module.os,"fdopen",
                      side_effect=OSError("SIMULATED_STREAM_FAILURE")), \
         patch.object(journal_module.os,"close",side_effect=tracked_close):
        with pytest.raises(OSError,match="SIMULATED_STREAM_FAILURE"):
            if writer=="blob":
                journal._store_text("large-snapshot\n"*1000)
            else:
                journal._rewrite()
    assert len(closed)==1
    assert (target.read_bytes() if target.exists() else None)==previous
    assert not list(target.parent.rglob("*.tmp"))


def test_untrusted_journal_jsonl_record_size_is_bounded_before_parsing(tmp_path, monkeypatch):
    folder=tmp_path / ".easychange"
    folder.mkdir()
    path=folder / "journal.jsonl"
    oversized='{"change_id":"CH1","description":"' + "x"*512 + '"}\n'
    path.write_text(oversized,encoding="utf-8")
    monkeypatch.setattr(journal_module,"_MAX_JOURNAL_RECORD_CHARS",128)
    with patch.object(journal_module.json,"loads",
                      side_effect=AssertionError("unbounded journal JSON parsed")) as loads:
        with pytest.raises(ValueError,match="JOURNAL_RECORD_TOO_LARGE"):
            Journal(path,"S1")
    loads.assert_not_called()
    assert path.read_text(encoding="utf-8")==oversized


def test_untrusted_journal_jsonl_record_must_be_object(tmp_path):
    path=tmp_path / ".easychange" / "journal.jsonl"
    path.parent.mkdir()
    path.write_text('[1,2,3]\n',encoding="utf-8")
    with pytest.raises(ValueError,match="JOURNAL_RECORD_NOT_OBJECT"):
        Journal(path,"S1")
    assert path.read_text(encoding="utf-8")=='[1,2,3]\n'


def test_journal_rejects_write_oversize_before_publishing_unreadable_record(tmp_path,monkeypatch):
    target=tmp_path/".easychange"/"journal.jsonl"
    journal=Journal(target,"S1")
    monkeypatch.setattr(journal_module,"_MAX_JOURNAL_RECORD_CHARS",512)
    with pytest.raises(ValueError,match="JOURNAL_RECORD_TOO_LARGE"):
        journal.record("A"*800,"sample.txt","before","after")
    assert journal.count==0
    assert journal._counter==0
    assert not target.exists()
    # An oversized input must not poison the next legitimate edit or restart.
    saved=journal.record("write","sample.txt","before","after")
    assert saved.change_id=="CH1"
    recovered=Journal(target,"S1")
    assert recovered.count==1
    assert recovered.get("CH1").after=="after"


def test_journal_append_fsync_precedes_in_memory_commit(tmp_path,monkeypatch):
    target=tmp_path/".easychange"/"journal.jsonl"
    journal=Journal(target,"S1")
    original_fsync=journal_module.os.fsync
    observed=[]

    def tracked(fd):
        observed.append((fd,journal.count))
        return original_fsync(fd)

    monkeypatch.setattr(journal_module.os,"fsync",tracked)
    saved=journal.record("write","sample.txt","before","after")
    assert saved.change_id=="CH1"
    assert observed and all(count==0 for _,count in observed)
    assert Journal(target,"S1").get(saved.change_id).after=="after"


def test_journal_rewrite_rejects_oversized_meta_without_damaging_prior_log(tmp_path,monkeypatch):
    target=tmp_path/".easychange"/"journal.jsonl"
    journal=Journal(target,"S1")
    journal.record("write","sample.txt","before","after")
    prior=target.read_bytes()
    journal._redo=[journal._entries[0]]
    monkeypatch.setattr(journal_module,"_MAX_JOURNAL_RECORD_CHARS",80)
    with pytest.raises(ValueError,match="JOURNAL_RECORD_TOO_LARGE"):
        journal._rewrite()
    assert target.read_bytes()==prior
    assert not list(target.parent.glob(".*.tmp"))


def test_new_journal_blob_and_rewrite_fsync_parent_directory(tmp_path,monkeypatch):
    import os
    import stat

    if os.name != "posix":
        pytest.skip("directory fsync applies to POSIX")
    folder=tmp_path/".easychange"
    journal=Journal(folder/"journal.jsonl","S1")
    calls=[]
    real_fsync=journal_module.os.fsync

    def observed(fd):
        calls.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real_fsync(fd)

    monkeypatch.setattr(journal_module.os,"fsync",observed)
    journal.record("write","note.txt","before","after")
    assert calls[-2:]==["file","dir"]
    reference=journal._store_text("large immutable snapshot\n"*300)
    assert reference.startswith("@zblob:")
    assert calls[-2:]==["file","dir"]
    journal._rewrite()
    assert calls[-2:]==["file","dir"]
    assert Journal(folder/"journal.jsonl","S1").count==1


def test_directory_fsync_unavailable_does_not_lose_atomic_journal(tmp_path,monkeypatch):
    import os
    import stat

    if os.name != "posix":
        return
    folder=tmp_path/".easychange"
    journal=Journal(folder/"journal.jsonl","S1")
    real_fsync=journal_module.os.fsync
    attempted=[]

    def directory_unsupported(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            attempted.append(True)
            raise OSError("DIRECTORY_SYNC_UNSUPPORTED")
        return real_fsync(fd)

    monkeypatch.setattr(journal_module.os,"fsync",directory_unsupported)
    journal.record("write","note.txt","before","after")
    reference=journal._store_text("important snapshot\n"*400)
    journal._rewrite()
    assert len(attempted)==3
    assert journal._load_text(reference)=="important snapshot\n"*400
    assert Journal(folder/"journal.jsonl","S1").count==1


def test_journal_restart_still_recovers_small_valid_entry(tmp_path):
    path=tmp_path / ".easychange" / "journal.jsonl"
    first=Journal(path,"S1")
    saved=first.record("write","hello.txt","before","after")
    reopened=Journal(path,"S1")
    assert reopened.count==1
    recovered=reopened.get(saved.change_id)
    assert recovered.before=="before"
    assert recovered.after=="after"


def test_simultaneous_journal_rewrites_do_not_collide_on_temp_path(tmp_path):
    target=tmp_path/".easychange"/"journal.jsonl"
    first=Journal(target,"S1")
    saved=first.record("write","demo.txt","before","after")
    second=Journal(target,"S1")
    barrier=threading.Barrier(2)
    temporary=[]
    guard=threading.Lock()
    original=journal_module.tempfile.mkstemp

    def simultaneous(*args,**kwargs):
        fd,name=original(*args,**kwargs)
        with guard:
            temporary.append(name)
        barrier.wait(timeout=10)
        return fd,name

    with patch.object(journal_module.tempfile,"mkstemp",
                      side_effect=simultaneous):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda journal:journal._rewrite(),(first,second)))
    restored=Journal(target,"S1")
    assert restored.count==1
    assert restored.get(saved.change_id).after=="after"
    assert len(set(temporary))==2
    assert not list(target.parent.glob(".journal.jsonl.*.tmp"))
