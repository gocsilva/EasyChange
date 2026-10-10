"""Standalone EasyChange diagnostics fail with JSON instead of broken connections."""
from __future__ import annotations

from unittest.mock import Mock, patch

from mcp_adapter.module_service import Handler


def handler(path):
    instance=object.__new__(Handler)
    instance.path=path
    instance.send=Mock()
    return instance


def test_status_failure_is_503_and_health_stays_independent():
    status=handler("/status")
    health=handler("/health")
    with patch("mcp_adapter.module_service.esp_online",return_value=False), \
         patch("mcp_adapter.module_service.module_status",
               side_effect=OSError("PRIVATE_PATH_OR_DEVICE")):
        status.do_GET()
        health.do_GET()
    status.send.assert_called_once_with(503,{"error":"MODULE_STATUS_UNAVAILABLE"})
    health.send.assert_called_once_with(
        200,{"ok":True,"id":"easychange","standalone":True})


def test_capabilities_failure_is_structured_and_does_not_leak_exception():
    client=handler("/capabilities")
    with patch("mcp_adapter.module_service.capabilities",
               side_effect=RuntimeError("secret-provider-internals")):
        client.do_GET()
    client.send.assert_called_once_with(
        503,{"error":"MODULE_CAPABILITIES_UNAVAILABLE"})


def test_normal_status_and_capabilities_keep_previous_shapes():
    for path,field,payload in (
        ("/status","module_status",{"ok":True}),
        ("/capabilities","capabilities",{"tools":[]}),
    ):
        client=handler(path)
        with patch("mcp_adapter.module_service.esp_online",return_value=False), \
             patch("mcp_adapter.module_service."+field,return_value=payload):
            client.do_GET()
        client.send.assert_called_once_with(200,payload)
