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
