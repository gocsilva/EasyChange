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


def test_refresh_reads_changed_files_in_bounded_batches(tmp_path, monkeypatch):
    import easychange.core.indexer as index_module

    count=index_module._INDEX_WORKERS*9+1
    for i in range(count):
        (tmp_path/f"script_{i:03}.py").write_text(
            f"def script_{i}():\n    return {i}\n",encoding="utf-8")

    batch_sizes=[]
    original_pool=index_module.ThreadPoolExecutor

    class MeasuredPool:
        def __init__(self,*args,**kwargs):
            self._real=original_pool(*args,**kwargs)
        def __enter__(self):
            self._real.__enter__()
            return self
        def __exit__(self,*args):
            return self._real.__exit__(*args)
        def map(self,fn,rows):
            batch_sizes.append(len(rows))
            return self._real.map(fn,rows)

    monkeypatch.setattr(index_module,"ThreadPoolExecutor",MeasuredPool)
    index=Indexer(tmp_path)
    result=index.refresh(force=True)
    assert result["indexed"]==count
    assert sum(batch_sizes)==count
    assert len(batch_sizes)>=3
    assert max(batch_sizes)<=index_module._INDEX_WORKERS*4
    with index._connect() as db:
        assert db.execute("SELECT count(*) FROM files").fetchone()[0]==count

    (tmp_path/"script_000.py").unlink()
    (tmp_path/"script_001.py").write_text("def changed():\n    pass\n",encoding="utf-8")
    index.invalidate()
    incremental=index.refresh()
    assert incremental["indexed"]==1
    assert incremental["removed"]==1
    with index._connect() as db:
        assert db.execute("SELECT count(*) FROM files").fetchone()[0]==count-1
        assert db.execute(
            "SELECT content FROM files WHERE path=?",("script_001.py",)
        ).fetchone()[0].startswith("def changed():")


def test_full_refresh_resolves_trusted_root_only_once(tmp_path):
    # Boundary checks still resolve every candidate path, but must not
    # re-resolve the *same* workspace root thousands of times on cold scans.
    for i in range(25):
        (tmp_path/f"entry_{i:03d}.py").write_text(
            f"def method_{i}():\n    return {i}\n",encoding="utf-8")
    index=Indexer(tmp_path)
    original_resolve=Path.resolve
    trusted_root_resolutions=[]

    def counted_resolve(path,*args,**kwargs):
        if path==tmp_path:
            trusted_root_resolutions.append(1)
        return original_resolve(path,*args,**kwargs)

    with patch.object(Path,"resolve",counted_resolve):
        result=index.refresh(force=True)
    assert result["indexed"]==25
    assert len(trusted_root_resolutions)==1
    with index._connect() as db:
        assert db.execute("SELECT count(*) FROM files").fetchone()[0]==25
