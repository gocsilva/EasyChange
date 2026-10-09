"""Job manifests must be bounded and staged independently across processes."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from easychange.core.process_service import ProcessService
import easychange.core.process_service as process_module


def _store(tmp_path):
    service=object.__new__(ProcessService)
    service.jobs_root=tmp_path
    service._job_write_lock=threading.RLock()
    return service


def test_job_manifest_reader_rejects_oversize_before_json_parse(tmp_path):
    jobs=_store(tmp_path)
    path=tmp_path/"JOBONE.json"
    path.write_bytes(b"{" + b"x"*256)
    jobs.MAX_JOB_MANIFEST_BYTES=32
    with patch.object(process_module.json,"loads",
                      side_effect=AssertionError("UNBOUNDED_JSON_PARSE")):
        assert jobs._read_job("JOBONE") is None
    assert path.stat().st_size==257


def test_job_manifest_writer_rejects_oversize_without_replacing_old_job(tmp_path):
    jobs=_store(tmp_path)
    original={"job_id":"JOBONE","state":"RUNNING"}
    jobs._write_job("JOBONE",original)
    path=tmp_path/"JOBONE.json"
    persisted=path.read_bytes()
    jobs.MAX_JOB_MANIFEST_BYTES=64
    with pytest.raises(ValueError,match="JOB_MANIFEST_TOO_LARGE"):
        jobs._write_job("JOBONE",{"job_id":"JOBONE","data":"x"*300})
    assert path.read_bytes()==persisted


def test_distinct_process_like_job_writers_do_not_reuse_staging_files(tmp_path):
    first=_store(tmp_path)
    second=_store(tmp_path)
    originals=process_module.tempfile.NamedTemporaryFile
    barrier=threading.Barrier(2)
    names=[]
    guard=threading.Lock()

    def intercepted(*args,**kwargs):
        handle=originals(*args,**kwargs)
        with guard:
            names.append(Path(handle.name))
        barrier.wait(timeout=10)
        return handle

    with patch.object(process_module.tempfile,"NamedTemporaryFile",
                      side_effect=intercepted):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda item:item[0]._write_job("JOBONE",item[1]),
                          ((first,{"job_id":"JOBONE","state":"RUNNING"}),
                           (second,{"job_id":"JOBONE","state":"FINISHED"}))))
    assert len(names)==2 and len(set(names))==2
    assert not any(path.exists() for path in names)
    result=json.loads((tmp_path/"JOBONE.json").read_text(encoding="utf-8"))
    assert result["state"] in {"RUNNING","FINISHED"}
    assert first._read_job("JOBONE")==result
