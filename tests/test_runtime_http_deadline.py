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


def test_http_readiness_with_zero_candidates_does_not_wait_at_all():
    service=object.__new__(ProjectRuntimeService)
    service._http_get=lambda *_args,**_kwargs: (
        (_ for _ in ()).throw(AssertionError("NO_ENDPOINT_SHOULD_BE_PROBED")))
    with patch("easychange.core.runtime_service.time.sleep",
               side_effect=AssertionError("EMPTY_POLL_MUST_NOT_SLEEP")) as sleep:
        result=service.wait_for_http([],timeout=35.0)
    assert result=={"ok":False,"error":"HTTP_STARTUP_TIMEOUT",
                    "last":{},"urls":[]}
    sleep.assert_not_called()


def test_http_readiness_avoids_duplicate_candidates_with_same_deadline():
    service=object.__new__(ProjectRuntimeService)
    current=[0.0]
    visited=[]
    def unavailable(url,*,timeout):
        visited.append(url)
        current[0]+=0.6
        return {"ok":False,"status":503,"url":url}

    service._http_get=unavailable
    first="http://127.0.0.1:5000/swagger"
    second="http://127.0.0.1:5000/openapi.json"
    urls=[first,first,second,first,second]
    with patch("easychange.core.runtime_service.time.monotonic",
               side_effect=lambda:current[0]), \
         patch("easychange.core.runtime_service.time.sleep") as sleep:
        result=service.wait_for_http(urls,timeout=1.0)
    assert visited==[first,second]
    assert result["error"]=="HTTP_STARTUP_TIMEOUT"
    assert result["urls"]==urls
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
        {"ok":True,"status":200,"url":"http://127.0.0.1:5000/swagger/index.html",
         "body":b"<html><div id='swagger-ui'></div></html>"},
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
            timeout=3.0,require_swagger=True)
    assert result["ok"] is True and result["status"]==200
    assert len(visited)==3
    sleep.assert_not_called()


def test_swagger_404_never_creates_success_evidence_or_screenshot(tmp_path):
    from unittest.mock import Mock
    service=object.__new__(ProjectRuntimeService)
    service.profile=lambda _id: {
        "swagger_candidates":["http://127.0.0.1:5000/swagger"]}
    service.wait_for_http=lambda urls,timeout,require_swagger: {
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


def test_swagger_content_validator_distinguishes_real_docs_from_http_200_pages():
    service=ProjectRuntimeService
    good=(
        b'{"openapi":"3.0.3","paths":{}}',
        b'{"swagger":"2.0","paths":{}}',
        b"<html><head><title>Swagger UI</title></head><div id='swagger-ui'></div></html>",
        b"<html><redoc spec-url='/openapi.json'></redoc></html>",
    )
    bad=(
        b"OK",
        b"<html><title>Sign in</title></html>",
        b'{"status":"healthy","ok":true}',
        b'{"openapi":"3.0.3"}',
        b'{"openapi":"not-a-version","paths":{}}',
        b"[]",
        b"",
    )
    for body in good:
        assert service._swagger_document_valid({"body":body}), body[:60]
    for body in bad:
        assert not service._swagger_document_valid({"body":body}), body[:60]
    assert not service._swagger_document_valid({
        "body":good[0],"truncated":True})


def test_http200_login_page_is_skipped_before_valid_openapi_document():
    service=object.__new__(ProjectRuntimeService)
    seen=[]
    def get(url,*,timeout):
        seen.append(url)
        return {"ok":True,"status":200,"url":url,"body": (
            b"<html><title>Login required</title></html>"
            if url.endswith("/swagger/index.html")
            else b'{"openapi":"3.0.3","paths":{"/acam225":{}}}')}
    service._http_get=get
    result=service.wait_for_http(
        ["http://127.0.0.1:5000/swagger/index.html",
         "http://127.0.0.1:5000/swagger/v1/swagger.json"],
        timeout=2.0,require_swagger=True)
    assert result["url"].endswith("swagger.json")
    assert len(seen)==2


def test_swagger_evidence_refuses_generic_200_before_writing_files(tmp_path):
    from unittest.mock import Mock
    service=object.__new__(ProjectRuntimeService)
    service.profile=lambda _: {
        "swagger_candidates":["http://127.0.0.1:5000/swagger/index.html"]}
    service.wait_for_http=lambda urls,timeout,require_swagger: {
        "ok":True,"status":200,"url":urls[0],
        "body":b"<html><title>Default application</title></html>"}
    service.evidence_root=tmp_path/"evidence"
    service.processes=Mock()
    service._browser_screenshot=Mock()
    outcome=service.swagger_evidence(
        process_id="audit",start_if_needed=False,screenshot=True)
    assert outcome["ok"] is False
    assert not service.evidence_root.exists()
    service._browser_screenshot.assert_not_called()


def test_swagger_html_validator_rejects_keyword_only_login_and_index_pages():
    validate=ProjectRuntimeService._swagger_document_valid
    bad=(
        b"<html><title>Login required for Swagger UI</title></html>",
        b"<html><p>Redoc documentation is available after login</p></html>",
        b"<html><a href='/swagger-ui'>Swagger UI docs</a></html>",
        b"<html><script src='/analytics.js'></script>"
        b"<p>SwaggerUIBundle(example) documentation text</p></html>",
        b"<html><div id='login'>Use swagger-ui-bundle.js later</div></html>",
    )
    for sample in bad:
        assert not validate({"body":sample}),sample
    good=(
        b"<html><div id='swagger-ui'></div></html>",
        b'<html><section class="documentation" id = "swagger-ui"></section></html>',
        b"<html><script src='/swagger-ui-bundle.js'></script></html>",
        b'<html><script src="/swagger-ui-bundle.min.js?v=1"></script></html>',
        b"<html><script>window.onload=function(){SwaggerUIBundle({});}</script></html>",
        b"<html><redoc spec-url='/openapi.json'></redoc></html>",
        b"<html><redocly-api-reference></redocly-api-reference></html>",
    )
    for sample in good:
        assert validate({"body":sample}),sample


def test_login_keyword_does_not_stop_swagger_candidate_discovery():
    service=object.__new__(ProjectRuntimeService)
    candidates=["http://127.0.0.1:5000/login",
                "http://127.0.0.1:5000/swagger/index.html"]
    visited=[]
    response_bodies=[
        b'<html><title>Login to Swagger UI</title><a href="/swagger-ui">docs</a></html>',
        b"<html><div id='swagger-ui'></div></html>",
    ]

    def synthetic_probe(url,*,timeout):
        visited.append(url)
        return {"ok":True,"status":200,"url":url,"body":response_bodies[len(visited)-1]}

    service._http_get=synthetic_probe
    result=service.wait_for_http(candidates,timeout=2,require_swagger=True)
    assert result["ok"] is True
    assert result["url"]==candidates[1]
    assert visited==candidates
