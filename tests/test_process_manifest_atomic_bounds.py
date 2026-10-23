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


def test_legacy_job_migration_bounds_reads_and_preserves_existing_targets(tmp_path):
    legacy=tmp_path/".easychange"/"jobs"
    legacy.mkdir(parents=True)
    target_root=tmp_path/"new-jobs"
    target_root.mkdir()
    small=b'{"job_id":"GOOD","state":"RUNNING"}'
    (legacy/"GOOD.json").write_bytes(small)
    huge=b"x"*1024
    (legacy/"LARGE.json").write_bytes(huge)
    (legacy/"SAVED.json").write_bytes(b'{"state":"OLD"}')
    (target_root/"SAVED.json").write_bytes(b'{"state":"NEW"}')

    jobs=_store(target_root)
    jobs.cwd=tmp_path
    jobs.MAX_JOB_MANIFEST_BYTES=128
    with patch.object(Path,"read_bytes",
                      side_effect=AssertionError("UNBOUNDED_LEGACY_JOB_READ")):
        jobs._migrate_legacy_jobs()
    assert (target_root/"GOOD.json").read_bytes()==small
    assert not (target_root/"LARGE.json").exists()
    assert (target_root/"SAVED.json").read_bytes()==b'{"state":"NEW"}'
    assert (legacy/"LARGE.json").read_bytes()==huge
    assert not list(target_root.glob("*.tmp"))


def test_startup_job_reconcile_selects_only_newest_and_counts_all(tmp_path):
    import os

    jobs=_store(tmp_path)
    for index in range(70):
        identifier=f"JOB{index:03}"
        path=tmp_path/(identifier+".json")
        path.write_text(json.dumps({"job_id":identifier,"state":"SUCCEEDED"}),
                        encoding="utf-8")
        stamp=1_700_000_000_000_000_000+index*1_000_000
        os.utime(path,ns=(stamp,stamp))
    visited=[]
    read=jobs._read_job
    def tracked(job_id):
        visited.append(job_id)
        return read(job_id)
    jobs._read_job=tracked

    report=jobs._reconcile_durable_jobs(limit=7)
    assert report["available"]==70
    assert report["checked"]==7
    assert report["running_unattached"]==0
    assert visited==[f"JOB{index:03}" for index in range(69,62,-1)]

    visited.clear()
    empty=jobs._reconcile_durable_jobs(limit=0)
    assert empty["available"]==70
    assert empty["checked"]==0
    assert visited==[]


def test_maintenance_preserves_unknown_queued_and_running_receipts_under_disk_pressure(tmp_path):
    import os
    import time

    root=tmp_path/"runtime"
    jobs_root=root/"jobs"
    process_root=root/"processes"
    jobs_root.mkdir(parents=True)
    process_root.mkdir()
    jobs=_store(jobs_root)
    jobs._processes={}
    jobs.root=process_root
    jobs.workspace_state_root=root
    jobs.max_runtime_bytes=0

    stale=time.time()-10*24*60*60
    for name,content in (
        ("UNKNOWN",b'not valid JSON'),
        ("QUEUED",b'{"state":"QUEUED"}'),
        ("RUNNING",b'{"state":"RUNNING"}'),
        ("DONE",b'{"state":"SUCCEEDED"}'),
    ):
        manifest=jobs_root/(name+".json")
        manifest.write_bytes(content)
        stdout=process_root/(name+".out")
        stdout.write_bytes(b"stdout evidence")
        os.utime(manifest,(stale,stale))
        os.utime(stdout,(stale,stale))

    report=jobs.maintenance(keep_finished=0)
    for name in ("UNKNOWN","QUEUED","RUNNING"):
        assert (jobs_root/(name+".json")).exists(),name
        assert (process_root/(name+".out")).exists(),name
    assert not (jobs_root/"DONE.json").exists()
    assert not (process_root/"DONE.out").exists()
    assert report["disk_over_budget"] is True


def test_unreadable_oversize_manifest_has_unknown_nonterminal_state(tmp_path):
    jobs=_store(tmp_path)
    jobs.MAX_JOB_MANIFEST_BYTES=64
    path=tmp_path/"TOOLARGE.json"
    path.write_bytes(b'{"state":"SUCCEEDED","padding":"' + b"x"*128 + b'"}')
    assert jobs._durable_job_state(path)=="UNKNOWN"
    assert path.exists()


def test_runtime_storage_stats_counts_files_incrementally_without_symlinks(tmp_path):
    root=tmp_path/"runtime"
    nested=root/"jobs"
    nested.mkdir(parents=True)
    (nested/"ONE.json").write_bytes(b"1234567")
    (nested/"TWO.out").write_bytes(b"ABCDE")
    jobs=_store(nested)
    jobs.workspace_state_root=root
    jobs.max_runtime_bytes=10
    report=jobs.storage_stats()
    assert report["files"]==2
    assert report["bytes"]==12
    assert report["over_budget"] is True
    assert report["root"]==str(root)


@pytest.mark.parametrize("job_id", [
    "CON", "PRN", "AUX", "NUL", "COM1", "LPT9",
    "JØB", "A" * 5000,
])
def test_job_id_preflight_rejects_windows_devices_unicode_and_overlong_names(tmp_path, job_id):
    """EC1 job identifiers must not resolve to devices or pathological paths."""
    jobs = _store(tmp_path)
    with pytest.raises(ValueError, match="INVALID_JOB_ID"):
        jobs._job_path(job_id)


def test_job_id_preflight_preserves_valid_ascii_ids(tmp_path):
    jobs = _store(tmp_path)
    assert jobs._job_path("J-abc_123") == tmp_path / "J-abc_123.json"


@pytest.mark.parametrize("raw", [
    '{"job_id":"JOBONE","state":"RUNNING","heartbeat_at":NaN}',
    '{"job_id":"JOBONE","state":"RUNNING","heartbeat_at":Infinity}',
    '{"job_id":"JOBONE","state":"RUNNING","heartbeat_at":1e999}',
    '{"job_id":"JOBONE","state":"RUNNING","state":"SUCCEEDED"}',
])
def test_job_manifest_reader_rejects_ambiguous_or_nonfinite_json(tmp_path, raw):
    jobs = _store(tmp_path)
    (tmp_path / "JOBONE.json").write_text(raw, encoding="utf-8")
    assert jobs._read_job("JOBONE") is None


def test_job_manifest_writer_rejects_nonfinite_metadata_without_file(tmp_path):
    jobs = _store(tmp_path)
    with pytest.raises(ValueError):
        jobs._write_job("JOBONE", {"state": "RUNNING", "heartbeat_at": float("nan")})
    assert not (tmp_path / "JOBONE.json").exists()
