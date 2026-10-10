"""Large code/assets must not be loaded entirely into the indexer memory."""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

from easychange.core.indexer import Indexer, MAX_INDEX_BYTES


def test_large_candidate_hashes_all_bytes_without_read_bytes(tmp_path):
    filename=tmp_path/"enormous.bin"
    data=(b"\0xyz" * (MAX_INDEX_BYTES//4+8192))
    filename.write_bytes(data)
    metadata=filename.stat()
    with patch.object(Path,"read_bytes",
                      side_effect=AssertionError("UNBOUNDED_READ")):
        rel,size,mtime,digest,content,symbols=Indexer._read_candidate(
            (filename,"enormous.bin",metadata.st_size,metadata.st_mtime_ns))
    assert rel=="enormous.bin"
    assert size==len(data)
    assert mtime==metadata.st_mtime_ns
    assert digest==hashlib.sha256(data).hexdigest()
    assert content is None
    assert symbols==[]


def test_small_candidate_still_extracts_symbols_and_hash(tmp_path):
    path=tmp_path/"simple.py"
    raw=b"def ping():\n    return 7\n"
    path.write_bytes(raw)
    st=path.stat()
    rel,size,_mtime,digest,content,symbols=Indexer._read_candidate(
        (path,"simple.py",st.st_size,st.st_mtime_ns))
    assert rel=="simple.py" and size==len(raw)
    assert digest==hashlib.sha256(raw).hexdigest()
    assert content==raw.decode()
    assert any(item[0]=="ping" for item in symbols)


def test_missing_candidate_still_returns_skip_receipt(tmp_path):
    path=tmp_path/"deleted.cs"
    assert Indexer._read_candidate((path,"deleted.cs",10,123)) == (
        "deleted.cs",10,123,"",None,[])
