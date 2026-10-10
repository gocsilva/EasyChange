"""Runtime profile discovery reads bounded JSON and validates configuration shapes."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from easychange.core.runtime_service import ProjectRuntimeService


def _read_profiles(tmp_path, data):
    root=tmp_path/".easychange"
    root.mkdir(exist_ok=True)
    path=root/"runtime_profiles.json"
    path.write_text(data,encoding="utf-8")
    runtime=object.__new__(ProjectRuntimeService)
    runtime.config_path=path
    runtime.workspace=SimpleNamespace(root_path=tmp_path)
    runtime.resolve_environment=lambda *_a,**_k: (
        {},{"resolved_env_keys":[],"unresolved_env_refs":[]})
    return runtime._configured_profiles()


def _valid_row():
    return {"id":"sample","kind":"dotnet","urls":["http://localhost:5000","invalid"],
            "swagger_candidates":["http://localhost:5000/swagger"],
            "run_argv":["dotnet","run"],"env":{},"inherit_env":False}


def test_valid_configured_runtime_profile_still_discovers_urls_and_argv(tmp_path):
    profiles=_read_profiles(tmp_path,json.dumps({"profiles":[_valid_row()]}))
    assert len(profiles)==1
    assert profiles[0]["id"]=="sample"
    assert profiles[0]["run_argv"]==["dotnet","run"]
    assert profiles[0]["urls"]==["http://localhost:5000"]
    assert profiles[0]["swagger_candidates"]==[
        "http://localhost:5000/swagger"]
    assert profiles[0]["inherit_env"] is False


def test_legacy_null_optional_url_collections_remain_accepted(tmp_path):
    row=_valid_row()
    row["urls"]=None
    row["swagger_candidates"]=None
    result=_read_profiles(tmp_path,json.dumps({"profiles":[row]}))
    assert len(result)==1
    assert result[0]["urls"]==[]
    assert result[0]["swagger_candidates"]==[]


def test_malformed_profile_fields_do_not_create_runnable_profiles(tmp_path):
    cases=(
        {"urls":"http://127.0.0.1:9000"},
        {"urls":{"http://127.0.0.1:9000":True}},
        {"swagger_candidates":"http://127.0.0.1:9000/swagger"},
        {"swagger_candidates":{"http://127.0.0.1:9000":True}},
        {"inherit_env":"false"},
        {"inherit_env":1},
        {"id":{"unexpected":"object"}},
        {"id":"A"*129},
    )
    for changes in cases:
        row={**_valid_row(),**changes}
        assert _read_profiles(tmp_path,json.dumps({"profiles":[row]}))==[]


def test_oversized_or_excessive_profile_data_is_rejected(tmp_path):
    huge='{"profiles":[]}'+(" "*1_048_577)
    with patch("easychange.core.runtime_service.json.loads",
               side_effect=AssertionError("PARSED_OVERSIZED_CONFIG")) as decode:
        assert _read_profiles(tmp_path,huge)==[]
    decode.assert_not_called()
    rows=[{**_valid_row(),"id":f"profile-{i}"} for i in range(257)]
    assert _read_profiles(tmp_path,json.dumps({"profiles":rows}))==[]


def test_deeply_nested_profile_config_fails_without_recursing_forever(tmp_path):
    malformed='{"profiles":'+("["*1500)+"0"+("]"*1500)+"}"
    assert _read_profiles(tmp_path,malformed)==[]


def test_profile_update_is_atomic_and_preserves_original_after_replace_failure(
        tmp_path):
    import pytest

    root=tmp_path/".easychange"
    root.mkdir()
    runtime=object.__new__(ProjectRuntimeService)
    runtime.workspace=SimpleNamespace(root_path=tmp_path)
    runtime.config_path=root/"runtime_profiles.json"
    runtime.resolve_environment=lambda *_a,**_k: (
        {},{"resolved_env_keys":[],"unresolved_env_refs":[]})

    runtime.configure_profile("first",run_argv=["python","--version"],
                              inherit_env=False)
    original=runtime.config_path.read_bytes()
    with patch("easychange.core.runtime_service.os.replace",
               side_effect=OSError("SIMULATED_REPLACE_FAILURE")):
        with pytest.raises(OSError,match="SIMULATED_REPLACE_FAILURE"):
            runtime.configure_profile("second",test_argv=["python","-m","pytest"])
    assert runtime.config_path.read_bytes()==original
    assert not list(root.glob(".runtime_profiles.json.*.tmp"))
    assert [row["id"] for row in runtime._configured_profiles()]==["first"]

    runtime.configure_profile("second",test_argv=["python","-m","pytest"])
    assert {row["id"] for row in runtime._configured_profiles()}=={"first","second"}


def test_runtime_profile_replacement_stays_owner_only_on_posix(tmp_path):
    import os
    import stat

    root=tmp_path/".easychange"
    root.mkdir()
    runtime=object.__new__(ProjectRuntimeService)
    runtime.workspace=SimpleNamespace(root_path=tmp_path)
    runtime.config_path=root/"runtime_profiles.json"
    runtime.resolve_environment=lambda *_a,**_k: (
        {},{"resolved_env_keys":[],"unresolved_env_refs":[]})
    runtime.configure_profile("private",env={"TEST_VALUE":"synthetic"},
                              inherit_env=False)
    assert runtime.config_path.is_file()
    if os.name=="posix":
        assert stat.S_IMODE(runtime.config_path.stat().st_mode)==0o600
    assert not list(root.glob(".runtime_profiles.json.*.tmp"))


def test_writer_keeps_malformed_existing_runtime_profiles_untouched(tmp_path):
    import pytest
    root=tmp_path/".easychange"
    root.mkdir()
    runtime=object.__new__(ProjectRuntimeService)
    runtime.workspace=SimpleNamespace(root_path=tmp_path)
    runtime.config_path=root/"runtime_profiles.json"
    runtime.resolve_environment=lambda *_a,**_k: (
        {},{"resolved_env_keys":[],"unresolved_env_refs":[]})
    cases=(
        b'{"profiles": [',
        json.dumps({"profiles":[_valid_row(), {"id":"broken","urls":"bad"}]}).encode(),
        json.dumps({"profiles":[_valid_row(),_valid_row()]}).encode(),
        json.dumps({"profiles":{"not":"a list"}}).encode(),
        b'{"profiles":[]}' + b" " * 1_048_577,
    )
    for contents in cases:
        runtime.config_path.write_bytes(contents)
        assert runtime._configured_profiles() == (
            [_read for _read in runtime._configured_profiles()]
        )  # tolerant discovery remains callable
        with pytest.raises(ValueError,match="RUNTIME_PROFILE_CONFIG_"):
            runtime.configure_profile("new",run_argv=["python","--version"])
        assert runtime.config_path.read_bytes()==contents
        assert not list(root.glob(".runtime_profiles.json.*.tmp"))


def test_writer_rejects_new_profile_id_the_reader_would_ignore(tmp_path):
    import pytest

    root=tmp_path/".easychange"
    root.mkdir()
    runtime=object.__new__(ProjectRuntimeService)
    runtime.workspace=SimpleNamespace(root_path=tmp_path)
    runtime.config_path=root/"runtime_profiles.json"
    runtime.resolve_environment=lambda *_a,**_k: (
        {},{"resolved_env_keys":[],"unresolved_env_refs":[]})
    runtime.configure_profile("valid",inherit_env=False)
    original=runtime.config_path.read_bytes()
    with pytest.raises(ValueError,match="PROFILE_ID_TOO_LONG"):
        runtime.configure_profile("x"*129)
    assert runtime.config_path.read_bytes()==original
    assert [r["id"] for r in runtime._configured_profiles()]==["valid"]


def test_writer_refuses_unreadable_oversized_config_without_replacing_old(
        tmp_path):
    import pytest

    root=tmp_path/".easychange"
    root.mkdir()
    runtime=object.__new__(ProjectRuntimeService)
    runtime.workspace=SimpleNamespace(root_path=tmp_path)
    runtime.config_path=root/"runtime_profiles.json"
    runtime.resolve_environment=lambda *_a,**_k: (
        {},{"resolved_env_keys":[],"unresolved_env_refs":[]})
    runtime.configure_profile("first",run_argv=["python","--version"])
    old=runtime.config_path.read_bytes()

    # Simulate merging a large historical profile without first materializing
    # a large file on disk. Streaming must terminate at the parser's budget.
    historical={"id":"legacy","env":{"LONG":"L"*1_100_000}}
    with patch.object(runtime,"_configured_profiles",return_value=[historical]):
        with pytest.raises(ValueError,match="RUNTIME_PROFILE_CONFIG_TOO_LARGE"):
            runtime.configure_profile("second",test_argv=["python","-m","pytest"])
    assert runtime.config_path.read_bytes()==old
    assert not list(root.glob(".runtime_profiles.json.*.tmp"))


def test_writer_refuses_more_profiles_than_reader_would_admit(tmp_path):
    import pytest

    root=tmp_path/".easychange"
    root.mkdir()
    runtime=object.__new__(ProjectRuntimeService)
    runtime.workspace=SimpleNamespace(root_path=tmp_path)
    runtime.config_path=root/"runtime_profiles.json"
    runtime.resolve_environment=lambda *_a,**_k: (
        {},{"resolved_env_keys":[],"unresolved_env_refs":[]})
    runtime.configure_profile("first",run_argv=["python","--version"])
    old=runtime.config_path.read_bytes()
    existing=[{"id":f"old-{index}"} for index in range(256)]
    with patch.object(runtime,"_configured_profiles",return_value=existing):
        with pytest.raises(ValueError,match="RUNTIME_PROFILE_CONFIG_TOO_LARGE"):
            runtime.configure_profile("new",build_argv=["dotnet","build"])
    assert runtime.config_path.read_bytes()==old
    assert not list(root.glob(".runtime_profiles.json.*.tmp"))


def test_parallel_profile_writers_preserve_distinct_updates(tmp_path):
    """Two runtime instances must serialize read+merge+replace, not only writes."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    folder=tmp_path/".easychange"
    folder.mkdir()
    gate=Barrier(8)

    def create(index):
        service=object.__new__(ProjectRuntimeService)
        service.workspace=SimpleNamespace(root_path=tmp_path)
        service.config_path=folder/"runtime_profiles.json"
        service.resolve_environment=lambda *_a,**_k: (
            {},{"resolved_env_keys":[],"unresolved_env_refs":[]})
        gate.wait(timeout=10)
        return service.configure_profile(
            f"parallel-{index}",test_argv=["python","-m","pytest"],
            inherit_env=False)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(create,range(8)))
    assert len(results)==8
    inspection=object.__new__(ProjectRuntimeService)
    inspection.workspace=SimpleNamespace(root_path=tmp_path)
    inspection.config_path=folder/"runtime_profiles.json"
    inspection.resolve_environment=lambda *_a,**_k: (
        {},{"resolved_env_keys":[],"unresolved_env_refs":[]})
    profiles=inspection._configured_profiles()
    assert {item["id"] for item in profiles}=={
        f"parallel-{index}" for index in range(8)}
    assert len(profiles)==8


def test_profile_lock_refuses_symlink_without_writing_config(tmp_path):
    import pytest

    folder=tmp_path/".easychange"
    folder.mkdir()
    service=object.__new__(ProjectRuntimeService)
    service.workspace=SimpleNamespace(root_path=tmp_path)
    service.config_path=folder/"runtime_profiles.json"
    service.resolve_environment=lambda *_a,**_k: (
        {},{"resolved_env_keys":[],"unresolved_env_refs":[]})
    outside=tmp_path/"outside.txt"
    outside.write_text("UNTOUCHED",encoding="utf-8")
    lock=folder/"runtime_profiles.json.lock"
    try:
        lock.symlink_to(outside)
    except (OSError,NotImplementedError):
        pytest.skip("symlink creation not permitted on this OS")
    with pytest.raises(RuntimeError,match="RUNTIME_PROFILE_LOCK_UNSAFE"):
        service.configure_profile("unsafe",test_argv=["python","-m","pytest"])
    assert outside.read_text(encoding="utf-8")=="UNTOUCHED"
    assert not service.config_path.exists()
