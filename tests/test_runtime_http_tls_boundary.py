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
            "easychange.core.runtime_service.urllib.request.urlopen",
            return_value=FakeResponse(b'{"ok":true}'),
        ) as fetch, patch(
            "easychange.core.runtime_service.ssl._create_unverified_context"
        ) as unverified:
            result=service._http_get(url)
        assert result["ok"] is True
        assert fetch.call_args.kwargs["context"] is None
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
            "easychange.core.runtime_service.urllib.request.urlopen",
            return_value=FakeResponse(b"healthy"),
        ) as fetch, patch(
            "easychange.core.runtime_service.ssl._create_unverified_context",
            return_value=marker,
        ) as unverified:
            result=service._http_get(url)
        assert result["ok"] is True
        assert fetch.call_args.kwargs["context"] is marker
        unverified.assert_called_once_with()


def test_invalid_url_fails_without_opening_network_or_tls_exception():
    service=object.__new__(ProjectRuntimeService)
    with patch("easychange.core.runtime_service.urllib.request.urlopen") as fetch:
        result=service._http_get("https://[invalid")
    assert result["ok"] is False
    assert result["error"] == "ValueError"
    fetch.assert_not_called()
