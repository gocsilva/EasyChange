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
