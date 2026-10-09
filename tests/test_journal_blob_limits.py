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
