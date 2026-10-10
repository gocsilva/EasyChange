"""MachineSession writes must stay atomic when two app instances share a workspace."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading
from unittest.mock import patch

import pytest

import easychange.core.state as state_module
from easychange.core.state import MachineSession


def test_concurrent_session_writers_use_distinct_staging_files(tmp_path):
    target=tmp_path/".easychange"/"session.json"
    first=MachineSession(target,"demo")
    second=MachineSession(target,"demo")
    first.data["current_file"]="first.cs"
    second.data["current_file"]="second.cs"
    original=state_module.tempfile.mkstemp
    barrier=threading.Barrier(2)
    temp_names=[]
    guard=threading.Lock()

    def simultaneous_create(*args,**kwargs):
        fd,name=original(*args,**kwargs)
        with guard:
            temp_names.append(name)
        barrier.wait(timeout=10)
        return fd,name

    with patch.object(state_module.tempfile,"mkstemp",
                      side_effect=simultaneous_create):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda obj:obj.save(),(first,second)))
    saved=json.loads(target.read_text(encoding="utf-8"))
    assert saved["current_file"] in ("first.cs","second.cs")
    assert saved["workspace"]=="demo"
    assert len(set(temp_names))==2
    assert list(target.parent.glob(".session.json.*.tmp"))==[]


def test_failed_atomic_replace_cleans_up_temp_and_keeps_previous_state(tmp_path):
    target=tmp_path/".easychange"/"session.json"
    session=MachineSession(target,"demo")
    session.data["line"]=3
    session.save()
    before=target.read_bytes()
    session.data["line"]=77
    with patch.object(state_module.os,"replace",
                      side_effect=OSError("DISK_UNAVAILABLE")):
        with pytest.raises(OSError,match="DISK_UNAVAILABLE"):
            session.save()
    assert target.read_bytes()==before
    assert list(target.parent.glob(".session.json.*.tmp"))==[]


def test_session_file_then_parent_directory_are_synced_in_order(tmp_path,monkeypatch):
    import os
    import stat

    if os.name!="posix":
        pytest.skip("directory fsync applies to POSIX")
    session=MachineSession(tmp_path/".easychange"/"session.json","demo")
    events=[]
    original_replace=state_module.os.replace
    original_fsync=state_module.os.fsync

    def observed_replace(source,destination):
        events.append("rename")
        return original_replace(source,destination)

    def observed_fsync(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return original_fsync(fd)

    monkeypatch.setattr(state_module.os,"replace",observed_replace)
    monkeypatch.setattr(state_module.os,"fsync",observed_fsync)
    session.save()
    assert events==["file","rename","directory"]
    assert MachineSession(session.path,"demo").data["session_id"]==session.data["session_id"]


def test_session_recovery_when_directory_fsync_is_unsupported(tmp_path,monkeypatch):
    import os
    import stat

    if os.name!="posix":
        return
    target=tmp_path/".easychange"/"session.json"
    session=MachineSession(target,"demo")
    original_fsync=state_module.os.fsync
    attempts=[]

    def unsupported_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            attempts.append(True)
            raise OSError("DIRECTORY_FSYNC_UNAVAILABLE")
        return original_fsync(fd)

    monkeypatch.setattr(state_module.os,"fsync",unsupported_directory)
    session.data["line"]=42
    session.save()
    assert attempts==[True]
    assert MachineSession(target,"demo").data["line"]==42


@pytest.mark.parametrize("invalid",("[1,2]","null","false",'"text"'))
def test_non_object_session_json_does_not_crash_workspace_start(tmp_path,invalid):
    target=tmp_path/".easychange"/"session.json"
    target.parent.mkdir()
    target.write_text(invalid,encoding="utf-8")
    session=MachineSession(target,"demo")
    assert session.data["workspace"]=="demo"
    assert isinstance(session.data["history"],list)
    assert target.read_text(encoding="utf-8")==invalid
