"""Same-second evidence runs must never overwrite one another."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import json
import pytest

from easychange.core.runtime_service import ProjectRuntimeService


def _service(tmp_path):
    service=object.__new__(ProjectRuntimeService)
    service.workspace=SimpleNamespace(root_path=tmp_path)
    service.evidence_root=tmp_path/".easychange"/"evidence"
    service.evidence_root.mkdir(parents=True,exist_ok=True)
    service.profile=lambda _id: {
        "id":"swagger-evidence","kind":"dotnet","run_argv":[],
        "swagger_candidates":["http://127.0.0.1:5051/swagger/index.html"],
    }
    service.profiles=lambda: [{"id":"local-profile"}]
    service.processes=SimpleNamespace(last_execution={"stdout":"initial","returncode":0})
    service._browser_screenshot=lambda _url,_target:None
    return service


def test_same_second_swagger_evidence_keeps_each_http_response(tmp_path):
    service=_service(tmp_path)
    counter=iter(("first","second"))
    def probe(*_args,**_kwargs):
        marker=next(counter)
        return {"ok":True,"status":200,
                "url":"http://127.0.0.1:5051/swagger/index.html",
                "content_type":"text/html",
                "body":("<div id='swagger-ui'>"+marker+"</div>").encode()}
    service.wait_for_http=probe
    same_second=datetime(2026,10,10,12,34,56,tzinfo=timezone.utc)
    with patch("easychange.core.runtime_service.datetime") as clock:
        clock.now.return_value=same_second
        first=service.swagger_evidence("job",screenshot=False,start_if_needed=False)
        second=service.swagger_evidence("job",screenshot=False,start_if_needed=False)
    assert first["ok"] and second["ok"]
    assert first["created_utc"]==second["created_utc"]
    assert first["evidence_dir"]!=second["evidence_dir"]
    first_data=(tmp_path/first["response_file"]).read_bytes()
    second_data=(tmp_path/second["response_file"]).read_bytes()
    assert b"first" in first_data and b"second" not in first_data
    assert b"second" in second_data and b"first" not in second_data


def test_same_second_generic_evidence_keeps_distinct_reports(tmp_path):
    service=_service(tmp_path)
    same_second=datetime(2026,10,10,12,34,56,tzinfo=timezone.utc)
    with patch("easychange.core.runtime_service.datetime") as clock:
        clock.now.return_value=same_second
        first=service.evidence("repeatable-audit")
        service.processes.last_execution={"stdout":"updated","returncode":3}
        second=service.evidence("repeatable-audit")
    assert first["evidence_dir"]!=second["evidence_dir"]
    first_data=json.loads((tmp_path/first["evidence_json"]).read_text())
    second_data=json.loads((tmp_path/second["evidence_json"]).read_text())
    assert first_data["last_execution"]["stdout"]=="initial"
    assert second_data["last_execution"]["stdout"]=="updated"


def test_forced_evidence_id_collision_cannot_overwrite_existing_run(tmp_path):
    service=_service(tmp_path)
    with patch("easychange.core.runtime_service.uuid.uuid4",
               return_value=SimpleNamespace(hex="same-id")):
        first=service.evidence("single-run")
        first_contents=(tmp_path/first["evidence_json"]).read_bytes()
        with pytest.raises(FileExistsError):
            service.evidence("single-run")
    assert (tmp_path/first["evidence_json"]).read_bytes()==first_contents
