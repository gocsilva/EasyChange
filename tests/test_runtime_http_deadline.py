"""HTTP readiness has a total deadline, not a new deadline per candidate."""
from __future__ import annotations

from unittest.mock import patch

from easychange.core.runtime_service import ProjectRuntimeService


def test_http_readiness_stops_between_candidates_after_total_deadline():
    service=object.__new__(ProjectRuntimeService)
    current=[0.0]
    observed=[]

    def unavailable(url, *, timeout):
        observed.append((url,timeout))
        current[0]+=0.6
        return {"ok":False,"url":url,"error":"TimeoutError"}

    service._http_get=unavailable
    urls=[f"http://127.0.0.1:{5000+i}/swagger" for i in range(20)]
    with patch("easychange.core.runtime_service.time.monotonic",
               side_effect=lambda:current[0]), \
         patch("easychange.core.runtime_service.time.sleep") as sleep:
        result=service.wait_for_http(urls,timeout=1.0)

    assert result["error"]=="HTTP_STARTUP_TIMEOUT"
    assert len(observed)==2
    assert observed[0][1] == 1.0
    assert 0 < observed[1][1] <= 0.4
    sleep.assert_not_called()


def test_http_readiness_returns_successful_probe_without_waiting_for_more():
    service=object.__new__(ProjectRuntimeService)
    seen=[]
    def healthy(url, *, timeout):
        seen.append((url,timeout))
        return {"ok":True,"status":200,"url":url}
    service._http_get=healthy
    with patch("easychange.core.runtime_service.time.sleep") as sleep:
        result=service.wait_for_http(
            ["http://127.0.0.1:5000/health","http://127.0.0.1:5001/health"],
            timeout=3.0)
    assert result["status"]==200
    assert len(seen)==1
    sleep.assert_not_called()


def test_swagger_readiness_ignores_404_and_unauthorized_candidates():
    service=object.__new__(ProjectRuntimeService)
    visited=[]
    replies=iter((
        {"ok":False,"status":404,"url":"http://127.0.0.1:5000/swagger"},
        {"ok":False,"status":401,"url":"http://127.0.0.1:5000/openapi.json"},
        {"ok":True,"status":200,"url":"http://127.0.0.1:5000/swagger/index.html"},
    ))
    def check(url, *, timeout):
        visited.append(url)
        return next(replies)
    service._http_get=check
    with patch("easychange.core.runtime_service.time.sleep") as sleep:
        result=service.wait_for_http(
            ["http://127.0.0.1:5000/swagger",
             "http://127.0.0.1:5000/openapi.json",
             "http://127.0.0.1:5000/swagger/index.html"],
            timeout=3.0)
    assert result["ok"] is True and result["status"]==200
    assert len(visited)==3
    sleep.assert_not_called()


def test_swagger_404_never_creates_success_evidence_or_screenshot(tmp_path):
    from unittest.mock import Mock
    service=object.__new__(ProjectRuntimeService)
    service.profile=lambda _id: {
        "swagger_candidates":["http://127.0.0.1:5000/swagger"]}
    service.wait_for_http=lambda urls,timeout: {
        "ok":False,"status":404,"url":urls[0],"body":b"Not Found"}
    service.processes=Mock()
    service._browser_screenshot=Mock()
    service.evidence_root=tmp_path/"evidence"
    response=service.swagger_evidence(
        process_id="audit",start_if_needed=False,screenshot=True)
    assert response["ok"] is False
    assert response["probe"]["status"]==404
    assert not service.evidence_root.exists()
    service._browser_screenshot.assert_not_called()
    service.processes.stop.assert_not_called()


def test_swagger_http_error_closes_socket_and_bounds_error_body():
    import io
    from urllib.error import HTTPError
    from unittest.mock import Mock

    url="http://127.0.0.1:5000/swagger/index.html"
    response_stream=io.BytesIO(b"not-found-response")
    error=HTTPError(url,404,"Not Found",
                    {"Content-Type":"text/plain"},response_stream)
    opener=Mock()
    opener.open.side_effect=error
    service=object.__new__(ProjectRuntimeService)
    with patch("easychange.core.runtime_service.urllib.request.build_opener",
               return_value=opener):
        result=service._http_get(url,timeout=0.5,max_bytes=5)
    assert result["status"]==404 and result["ok"] is False
    assert result["truncated"] is True and result["body"]==b"not-f"
    assert response_stream.closed
