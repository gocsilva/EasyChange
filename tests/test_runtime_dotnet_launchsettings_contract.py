"""Malformed or huge launchSettings.json must not abort project discovery."""
from __future__ import annotations

import json

from easychange.core.runtime_service import ProjectRuntimeService


def _setup(tmp_path, payload):
    project=tmp_path/"SampleApi"/"SampleApi.csproj"
    project.parent.mkdir(parents=True)
    project.write_text('<Project Sdk="Microsoft.NET.Sdk.Web" />',encoding="utf-8")
    settings=project.parent/"Properties"/"launchSettings.json"
    settings.parent.mkdir()
    settings.write_text(payload,encoding="utf-8")
    return project,settings


def test_dotnet_urls_from_valid_profiles_are_preserved(tmp_path):
    project,_=_setup(tmp_path,json.dumps({"profiles":{
        "https":{"applicationUrl":"https://localhost:7041;http://localhost:5001/"},
        "other":{"applicationUrl":"https://localhost:7041"},
        "no-url":{"launchBrowser":False},
    }}))
    service=object.__new__(ProjectRuntimeService)
    assert service._dotnet_urls(project)==[
        "https://localhost:7041","http://localhost:5001"]


def test_bad_launchsettings_root_and_profile_shapes_fail_closed(tmp_path):
    service=object.__new__(ProjectRuntimeService)
    for malformed in (
        "null","[]","17",'"text"',
        '{"profiles":[]}',
        '{"profiles":"bad"}',
        '{"profiles":{"dev":{"applicationUrl":["http://localhost:5000"]}}}',
        '{"profiles":{"dev":{"applicationUrl":{"host":"http://localhost"}}}}',
        "{incomplete",
    ):
        project,_=_setup(tmp_path,malformed)
        assert service._dotnet_urls(project)==[],malformed


def test_oversized_launchsettings_does_not_parse_entire_file(tmp_path):
    project,settings=_setup(tmp_path,"{" + '"padding":"' + "x"*1_100_000 + '"}')
    assert settings.stat().st_size>1_048_576
    service=object.__new__(ProjectRuntimeService)
    assert service._dotnet_urls(project)==[]


def test_launchsettings_limits_profile_and_url_cardinality(tmp_path):
    service=object.__new__(ProjectRuntimeService)
    many={f"p{i}":{"applicationUrl":"http://localhost:5000"}
          for i in range(129)}
    project,_=_setup(tmp_path,json.dumps({"profiles":many}))
    assert service._dotnet_urls(project)==[]

    sixteen={f"p{i}":{"applicationUrl":f"http://localhost:{5000+i}"}
             for i in range(30)}
    project,_=_setup(tmp_path,json.dumps({"profiles":sixteen}))
    assert len(service._dotnet_urls(project))==16
