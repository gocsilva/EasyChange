"""Preserve evidence at the Windows/ATT boundary without inferring policy."""
import asyncio
import re
import sys
import types
from unittest.mock import AsyncMock

import pytest

from easychange.remote import gatt_client


@pytest.mark.parametrize("status,att,label", [
    (0, None, "SUCCESS"), (1, None, "UNREACHABLE"),
    (2, 2, "PROTOCOL_ERROR"), (3, None, "ACCESS_DENIED"),
])
def test_native_read_retains_exact_status_and_does_not_repeat_read(monkeypatch, status, att, label):
    monkeypatch.setattr(gatt_client, "_on_windows", lambda: True)
    bluetooth = types.ModuleType("winrt.windows.devices.bluetooth")
    bluetooth.BluetoothCacheMode = types.SimpleNamespace(UNCACHED=999)
    monkeypatch.setitem(sys.modules, "winrt.windows.devices.bluetooth", bluetooth)
    native_read = AsyncMock(return_value=types.SimpleNamespace(
        status=status, protocol_error=att, value=b"BMO-GATT-DIAG-1"))
    attribute = types.SimpleNamespace(obj=types.SimpleNamespace(read_value_with_cache_mode_async=native_read))
    client = types.SimpleNamespace(read_gatt_char=AsyncMock(
        side_effect=AssertionError("Must not duplicate native read")))
    value, evidence = asyncio.run(gatt_client._gatt_probe_read(client, attribute))
    assert value == (b"BMO-GATT-DIAG-1" if status == 0 else None)
    assert f"WINRT_STATUS={label}" in evidence
    assert ("ATT=0x02" if att == 2 else "ATT=NONE") in evidence
    native_read.assert_awaited_once_with(999)
    client.read_gatt_char.assert_not_called()
    assert "BMO-GATT-DIAG-1" not in evidence


def test_exception_evidence_preserves_signed_hresult_and_att_without_exception_payload():
    exc = RuntimeError("secret payload A1:B2:C3:D4:E5:F6")
    exc.protocol_error = 3
    exc.hresult = -2147024891
    exc.winerror = 5
    evidence = gatt_client._gatt_exception_evidence(exc)
    assert "ATT=0x03" in evidence
    assert "HRESULT=0x80070005" in evidence
    assert "WINERROR=0x00000005" in evidence
    assert "payload" not in evidence and "A1:B2" not in evidence


def test_bleak3_code_property_is_retained_and_unrelated_code_is_not_att():
    BleakGATTProtocolError = type("BleakGATTProtocolError", (Exception,), {"code": 3})
    exc = BleakGATTProtocolError("opaque")
    assert gatt_client._is_cccd_write_denied(exc)
    assert "ATT=0x03" in gatt_client._gatt_exception_evidence(exc)
    other = RuntimeError("opaque")
    other.code = 3
    assert "ATT=" not in gatt_client._gatt_exception_evidence(other)


@pytest.mark.parametrize("device_status,confirmed", [
    ("WINRT_ACCESS_STATUS=DENIED_BY_USER", True),
    ("WINRT_ACCESS_STATUS=DeniedBySystem", True),
    ("WINRT_ACCESS_QUERY_FAILED:PermissionDenied", False),
    ("WINRT_ACCESS_STATUS=ALLOWED", False),
    ("WINRT_ACCESS_INFORMATION_UNAVAILABLE", False),
])
def test_policy_verdict_requires_explicit_device_access_status(device_status, confirmed):
    verdict = gatt_client._gatt_verdict(
        stage="INSCRICAO_GATT", winrt_status=device_status,
        read_probe="DIAG_READ_FAILED:READ_API=WINRT_UNCACHED;WINRT_STATUS=ACCESS_DENIED;ATT=NONE",
        cccd_probe="CCCD_READ_FAILED:PermissionError:Access Denied",
        notification_error="exception=BleakGATTProtocolError;ATT=0x03",
        metadata="service=1,notify_char=1,notify=1,cccd=1", connected=True)
    assert (verdict["codigo"] == "WINRT_SERVICE_ACCESS_DENIED") is confirmed
    assert verdict["bloqueio_windows"].startswith("CONFIRMADO_") is confirmed
    assert verdict["evidencia_att"] == "ATT_0X03_CONFIRMADO"
    assert verdict["politica_corporativa"] == "NAO_IDENTIFICADA_PELA_API"
    if confirmed:
        assert "Parar repeticoes de firmware" in verdict["proximo_teste"]
    else:
        assert verdict["codigo"] == "ACCESS_DENIED_READ_AND_ATT03_CCCD"


def test_runtime_log_records_diagnostic_schema_and_own_source_fingerprint():
    bridge = gatt_client.EasyChangeGattClient()
    text = "\n".join(row["message"] for row in bridge.diagnostics.snapshot()["events"])
    assert "GATT_CLIENT_DIAG_SCHEMA=3" in text
    assert re.search(r"GATT_CLIENT_SOURCE_SHA256=[0-9a-f]{16}", text)
    assert "EasyChange=" in text and "WindowsBuild=" in text


def test_confirmed_windows_denial_stops_automatic_retries_before_att_io(monkeypatch):
    class Client:
        services = None
        mtu_size = 247
        is_connected = False
        def __init__(self, *_args, **_kwargs):
            pass
        async def __aenter__(self):
            self.is_connected = True
            return self
        async def __aexit__(self, *_args):
            self.is_connected = False
        async def start_notify(self, *_args):
            raise AssertionError("Explicit Windows denial must stop before CCCD I/O")
    bleak = types.ModuleType("bleak")
    bleak.BleakClient, bleak.BleakScanner = Client, object
    monkeypatch.setitem(sys.modules, "bleak", bleak)
    monkeypatch.setattr(gatt_client, "_winrt_custom_service_access_status",
                        lambda _client: "WINRT_ACCESS_STATUS=DENIED_BY_SYSTEM")
    probe = AsyncMock(side_effect=AssertionError("Must stop before read"))
    monkeypatch.setattr(gatt_client, "_custom_service_read_probe", probe)
    bridge = gatt_client.EasyChangeGattClient()
    bridge._resolve_target = AsyncMock(return_value=object())
    waits = []
    async def wait_for_operator(seconds):
        waits.append(seconds)
        bridge._stop.set()
    bridge._retry_delay = wait_for_operator
    asyncio.run(bridge._run())
    assert bridge._attempt == 1 and waits == [None]
    assert bridge._last_stage == "WINDOWS_ACCESS_ACTION_REQUIRED"
    assert bridge._last_verdict["codigo"] == "WINRT_SERVICE_ACCESS_DENIED"
    assert bridge._accepted is False
    probe.assert_not_awaited()
