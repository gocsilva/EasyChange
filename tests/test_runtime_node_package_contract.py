"""Node profile discovery must tolerate untrusted package metadata."""
from __future__ import annotations
import json

from easychange.core.runtime_service import ProjectRuntimeService


def _profiles(tmp_path, package):
    path=tmp_path/"package.json"
    path.write_text(package,encoding="utf-8")
    service=object.__new__(ProjectRuntimeService)
    service.workspace=type("Workspace",(),{"root_path":tmp_path})()
    return service._node_profiles()


def test_node_profile_discovers_valid_npm_scripts_without_executing_them(tmp_path):
    profiles=_profiles(tmp_path,json.dumps({
        "scripts":{"dev":"vite","start":"node app.js","test":"vitest"}}))
    assert len(profiles)==1
    assert profiles[0]["run_argv"]==["npm","run","dev"]
    assert profiles[0]["test_argv"]==["npm","test","--","--run"]
    assert profiles[0]["runnable"] is True


def test_node_profiles_ignore_non_object_package_or_scripts(tmp_path):
    for payload in ("[]","null",'"dev test"',"42","{invalid"):
        assert _profiles(tmp_path,payload)==[]
    for scripts in ("dev test",["dev","test"],True,None,
                    {"dev":False,"test":[]},{"dev":"   ","start":None}):
        result=_profiles(tmp_path,json.dumps({"scripts":scripts}))
        assert result[0]["run_argv"] is None
        assert result[0]["test_argv"] is None
        assert result[0]["runnable"] is False


def test_node_oversized_package_does_not_expand_json_in_memory(tmp_path):
    huge=json.dumps({"scripts":{"dev":"npm run web"},
                     "unused":"x"*1_100_000})
    assert len(huge)>1_048_576
    assert _profiles(tmp_path,huge)==[]


def test_node_start_script_can_be_used_when_dev_is_not_executable(tmp_path):
    result=_profiles(tmp_path,json.dumps({
        "scripts":{"dev":"","start":"node index.js","test":"pytest"}}))
    assert result[0]["run_argv"]==["npm","run","start"]
    assert result[0]["test_argv"]==["npm","test","--","--run"]
