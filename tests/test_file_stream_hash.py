"""Large workspace baselines must hash incrementally without unbounded reads."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from unittest.mock import patch

import pytest

from easychange.core.file_service import ExternalChangeError, FileService
from easychange.core.workspace import Workspace


def test_hash_stream_never_requests_unbounded_input():
    calls=[]
    class Tracked(io.BytesIO):
        def read(self,size=-1):
            calls.append(size)
            if size < 0:
                raise AssertionError("UNBOUNDED_READ")
            return super().read(size)

    payload=b"f"*(1024*1024*2+123)
    with patch.object(Path,"open",return_value=Tracked(payload)):
        digest=FileService._hash_file_stream(Path("synthetic.large"))
    assert digest==hashlib.sha256(payload).hexdigest()
    assert calls==[1024*1024]*4


def test_file_hash_baseline_and_delete_do_not_load_large_binary_file(tmp_path):
    target=tmp_path/"large.bin"
    content=b"X"*(1024*1024*3)+b"END"
    target.write_bytes(content)
    files=FileService(Workspace.open(tmp_path))
    with patch.object(Path,"read_bytes",
                      side_effect=AssertionError("WHOLE_FILE_READ_FORBIDDEN")):
        assert files.hash("large.bin")==hashlib.sha256(content).hexdigest()
        assert files.remember("large.bin")=="large.bin"
        target.write_bytes(content+b"MODIFIED")
        with pytest.raises(ExternalChangeError):
            files.delete("large.bin")
        assert target.exists()
        assert files.remember("large.bin",replace=True)=="large.bin"
        files.delete("large.bin")
    assert not target.exists()


def test_text_edit_and_append_use_bounded_reads_not_path_read_bytes(tmp_path):
    target=tmp_path/"sample.txt"
    target.write_text("alpha\n",encoding="utf-8")
    files=FileService(Workspace.open(tmp_path))
    with patch.object(Path,"read_bytes",
                      side_effect=AssertionError("UNBOUNDED_TEXT_READ")):
        assert files.read("sample.txt")["lines"]==["1|alpha"]
        assert files.write("sample.txt","beta\n")["verified"] is True
        assert files.append("sample.txt","gamma")["appended_chars"]==5
        files.replace("sample.txt","gamma","delta")
    assert target.read_text(encoding="utf-8")=="beta\ndelta"


def test_large_text_append_is_rejected_before_allocating_full_file(tmp_path):
    from easychange.core.file_service import MAX_TEXT_FILE_BYTES
    target=tmp_path/"huge.txt"
    target.write_bytes(b"x"*(MAX_TEXT_FILE_BYTES+1))
    files=FileService(Workspace.open(tmp_path))
    with patch.object(Path,"read_bytes",
                      side_effect=AssertionError("UNBOUNDED_APPEND_READ")):
        with pytest.raises(ValueError,match="text write limit"):
            files.append("huge.txt","should-not-write")
    assert target.stat().st_size==MAX_TEXT_FILE_BYTES+1


def test_text_fd_read_is_bounded_even_if_stat_was_stale():
    from easychange.core.file_service import MAX_TEXT_FILE_BYTES

    called=[]
    class HugeFile(io.BytesIO):
        def read(self,n=-1):
            called.append(n)
            if n<0:
                raise AssertionError("UNBOUNDED_FILE_DESCRIPTOR_READ")
            return super().read(n)
    with patch.object(Path,"open",return_value=HugeFile(b"x"*(MAX_TEXT_FILE_BYTES+2))):
        raw=FileService._read_limited_text(Path("stale.txt"))
    assert len(raw)==MAX_TEXT_FILE_BYTES+1
    assert called==[MAX_TEXT_FILE_BYTES+1]
