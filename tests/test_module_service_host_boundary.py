"""Reject DNS-rebound or ambiguous Host headers on the actual local handler."""
from __future__ import annotations

from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock

from mcp_adapter.module_service import Handler, _trusted_local_host


def _handler(hosts, port=8784):
    handler=object.__new__(Handler)
    handler.server=SimpleNamespace(server_address=("127.0.0.1",port))
    headers=Message()
    for host in hosts:
        headers.add_header("Host",host)
    handler.headers=headers
    return handler


def test_local_host_allows_only_matching_loopback_authority():
    for value in ("127.0.0.1:8784","localhost:8784","LOCALHOST:8784"):
        assert _trusted_local_host(_handler([value])) is True

    for values in (
        [],
        ["attacker.example:8784"],
        ["127.0.0.1:8784.attacker.example"],
        ["127.0.0.1:8784@attacker.example"],
        ["127.0.0.1:8785"],
        ["127.0.0.1"],
        ["localhost:8784","attacker.example:8784"],
    ):
        assert _trusted_local_host(_handler(values)) is False

    assert not _trusted_local_host(_handler(["localhost:8784"],port=0))


def test_rejected_host_never_reaches_get_or_post_service_handlers():
    for route in ("/health","/v1/attach"):
        handler=_handler(["attacker.example:8784"])
        handler.path=route
        handler.send=Mock()
        if route=="/health":
            handler.do_GET()
        else:
            handler.do_POST()
        handler.send.assert_called_once_with(403,{"error":"INVALID_HOST"})
