"""Only a real loopback HTTPS hostname may use the local dev TLS exception."""
from __future__ import annotations

import io
from unittest.mock import patch

from easychange.core.runtime_service import ProjectRuntimeService


class FakeResponse(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}


def test_external_prefix_lookalikes_keep_certificate_verification():
    service=object.__new__(ProjectRuntimeService)
    for url in (
        "https://localhost.attacker.example/status",
        "https://127.0.0.1.attacker.example/health",
        "https://example.com/health",
    ):
        with patch(
            "easychange.core.runtime_service.urllib.request.build_opener",
        ) as factory, patch(
            "easychange.core.runtime_service.ssl._create_unverified_context"
        ) as unverified:
            factory.return_value.open.return_value=FakeResponse(b'{"ok":true}')
            result=service._http_get(url)
        assert result["ok"] is True
        assert factory.return_value.open.call_args.kwargs["timeout"] == 4.0
        assert any(type(h).__name__=="HTTPSHandler" and h._context is None
                   for h in factory.call_args.args)
        unverified.assert_not_called()


def test_actual_loopback_https_preserves_local_development_behavior():
    service=object.__new__(ProjectRuntimeService)
    for url in (
        "https://localhost:5051/health",
        "https://127.0.0.1:7051/swagger",
        "https://[::1]:5051/status",
    ):
        marker=object()
        with patch(
            "easychange.core.runtime_service.urllib.request.build_opener",
        ) as factory, patch(
            "easychange.core.runtime_service.ssl._create_unverified_context",
            return_value=marker,
        ) as unverified:
            factory.return_value.open.return_value=FakeResponse(b"healthy")
            result=service._http_get(url)
        assert result["ok"] is True
        assert any(type(h).__name__=="HTTPSHandler" and h._context is marker
                   for h in factory.call_args.args)
        unverified.assert_called_once_with()


def test_invalid_url_fails_without_opening_network_or_tls_exception():
    service=object.__new__(ProjectRuntimeService)
    with patch("easychange.core.runtime_service.urllib.request.build_opener") as fetch:
        result=service._http_get("https://[invalid")
    assert result["ok"] is False
    assert result["error"] == "ValueError"
    fetch.assert_not_called()


def test_same_origin_redirect_is_allowed_but_off_origin_is_refused():
    from urllib.request import Request
    from easychange.core.runtime_service import _SameOriginRuntimeRedirect

    policy=_SameOriginRuntimeRedirect()
    request=Request("https://localhost:5051/swagger")
    local=policy.redirect_request(
        request,None,302,"Found",{},"https://localhost:5051/swagger/index.html")
    assert local is not None
    assert local.full_url=="https://localhost:5051/swagger/index.html"
    for escaped in (
        "https://localhost.attacker.example/swagger",
        "https://localhost:5052/swagger",
        "http://localhost:5051/swagger",
        "https://other.example.com/",
        "https://localhost:5051@evil.example/",
    ):
        assert policy.redirect_request(request,None,302,"Found",{},escaped) is None


def test_swagger_http_probe_rejects_file_and_embedded_credentials():
    service=object.__new__(ProjectRuntimeService)
    forbidden=(
        "file:///tmp/private.txt",
        "ftp://example.com/swagger",
        "data:text/plain,not-http",
        "https://name:password@example.com/swagger",
        "http:///swagger",
        "https://",
    )
    with patch("easychange.core.runtime_service.urllib.request.build_opener") as factory:
        for url in forbidden:
            result=service._http_get(url)
            assert result["ok"] is False, url
            assert result["error"] == "ValueError", url
            assert "INVALID_HTTP_PROBE_URL" in result["message"], url
    factory.assert_not_called()
