"""Swagger evidence must distinguish HTTP readiness from actual visual proof."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from easychange.core.runtime_service import ProjectRuntimeService


def _service(root: Path):
    root.mkdir(parents=True,exist_ok=True)
    service=object.__new__(ProjectRuntimeService)
    service.workspace=SimpleNamespace(root_path=root)
    service.evidence_root=root/".easychange"/"evidence"
    service.evidence_root.mkdir(parents=True)
    service.profile=lambda _: {
        "id":"swagger-test","kind":"dotnet","run_argv":[],
        "swagger_candidates":["http://127.0.0.1:5051/swagger/index.html"]}
    service.wait_for_http=lambda *_args,**_kw: {
        "ok":True,"status":200,"url":"http://127.0.0.1:5051/swagger/index.html",
        "content_type":"text/html","body":b"<html>swagger</html>"}
    return service


def test_missing_requested_screenshot_does_not_count_as_complete_evidence(tmp_path):
    service=_service(tmp_path/"needs-visual")
    service._browser_screenshot=lambda url,target: None
    result=service.swagger_evidence(
        process_id="test-no-process",screenshot=True,start_if_needed=False)
    assert result["ok"] is False
    assert result["error"]=="SWAGGER_SCREENSHOT_NOT_CAPTURED"
    assert result["http_verified"] is True
    assert result["screenshot_requested"] is True
    assert result["screenshot_captured"] is False
    assert result["screenshot_file"] is None
    response=service.workspace.root_path/result["response_file"]
    assert response.read_bytes()==b"<html>swagger</html>"
    report=service.workspace.root_path/result["report"]
    assert report.is_file()
    metadata=report.parent/"evidence.json"
    saved=json.loads(metadata.read_text(encoding="utf-8"))
    assert saved["http_verified"] is True
    assert saved["screenshot_captured"] is False


def test_http_only_evidence_can_succeed_when_no_screenshot_requested(tmp_path):
    service=_service(tmp_path/"http-only")
    result=service.swagger_evidence(
        process_id="test-no-process",screenshot=False,start_if_needed=False)
    assert result["ok"] is True
    assert result["http_verified"] is True
    assert result["screenshot_requested"] is False
    assert result["screenshot_captured"] is False
    assert "error" not in result
