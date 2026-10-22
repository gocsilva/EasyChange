import asyncio
import queue
from unittest.mock import AsyncMock

from easychange.remote.gatt_client import EasyChangeGattClient
from easychange.remote.gatt_security import GattHandshake
from easychange.remote.gatt_wire import seal_json, open_sealed

def test_client_does_not_connect_or_accept_commands_implicitly():
    bridge = EasyChangeGattClient()
    assert bridge.status()["secure"] is False
    assert bridge.commands.empty()
    assert bridge._session is None

def test_response_without_pairing_not_published():
    bridge = EasyChangeGattClient()
    bridge.publish_result("QABCDEF", {"ok":True})
    assert bridge.commands.empty()


def _sessions():
    host = GattHandshake("host")
    remote = GattHandshake("remote")
    sas = host.receive_peer(remote.public_key)
    assert sas == remote.receive_peer(host.public_key)
    return host.approve(sas), remote.approve(sas)


def test_remote_sends_authenticated_receipt_before_accepting_commands():
    host_session, remote_session = _sessions()
    bridge = EasyChangeGattClient()
    bridge._session = remote_session
    bridge._send = AsyncMock()
    asyncio.run(bridge._message(seal_json(host_session, {"t": "confirm"})))
    assert bridge._accepted is True
    assert bridge._send.await_count == 1
    response = bridge._send.await_args.args[0]
    assert open_sealed(host_session, response) == {"t": "confirmed"}


def test_failed_confirmation_receipt_leaves_remote_unpaired():
    host_session, remote_session = _sessions()
    bridge = EasyChangeGattClient()
    bridge._session = remote_session
    bridge._send = AsyncMock(side_effect=ConnectionError("BLE_NOT_CONNECTED"))
    asyncio.run(bridge._message(seal_json(host_session, {"t": "confirm"})))
    assert bridge._accepted is False
    assert bridge.commands.empty()


import sys
import types
from unittest.mock import Mock

import pytest
import easychange.remote.gatt_client as gatt_client


def test_ble_address_conversion_rejects_invalid():
    assert gatt_client._ble_address(0xA1B2C3D4E5F6) == "A1:B2:C3:D4:E5:F6"
    for invalid in (0, -1, 1 << 49, True):
        with pytest.raises(ValueError):
            gatt_client._ble_address(invalid)


def _fake_winrt_pair(monkeypatch, *, entries):
    """Fake Windows pairing database, not advertisements nor Bluetooth hardware."""
    module_names = [
        "bleak", "bleak.backends", "bleak.backends.device",
        "winrt", "winrt.windows", "winrt.windows.devices",
        "winrt.windows.devices.bluetooth", "winrt.windows.devices.enumeration",
    ]
    modules = {name: types.ModuleType(name) for name in module_names}
    for name, value in modules.items():
        monkeypatch.setitem(sys.modules, name, value)

    class FakeBLEDevice:
        def __init__(self, address, name, details):
            self.address, self.name, self.details = address, name, details

    class FakeLE:
        def __init__(self, address):
            self.bluetooth_address = address
            self.closed = False

        def close(self):
            self.closed = True

        @classmethod
        def get_device_selector_from_pairing_state(cls, paired):
            assert paired is True
            return "PAIRED_LE_ONLY"

        @classmethod
        async def from_id_async(cls, device_id):
            return devices.get(device_id)

    class FakeInfo:
        @staticmethod
        async def find_all_async(selector):
            assert selector == "PAIRED_LE_ONLY"
            return [types.SimpleNamespace(name=name, id=identity) for name, identity, _ in entries]

    devices = {identity: FakeLE(address) for name, identity, address in entries}
    modules["bleak.backends.device"].BLEDevice = FakeBLEDevice
    modules["winrt.windows.devices.bluetooth"].BluetoothLEDevice = FakeLE
    modules["winrt.windows.devices.enumeration"].DeviceInformation = FakeInfo
    monkeypatch.setattr(gatt_client, "_on_windows", lambda: True)
    return devices, FakeBLEDevice


def test_resolves_paired_windows_hid_without_scanning(monkeypatch):
    devices, FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[
        ("OTHER_DEVICE", "other", 0x111111111111),
        ("BT KeyboardMouse E", "esp", 0xA1B2C3D4E5F6),
    ])
    monkeypatch.delenv("EASYCHANGE_BLE_DEVICE_ADDRESS", raising=False)

    class RejectScan:
        @staticmethod
        async def discover(**_kwargs):
            raise AssertionError("Already paired HID must not need advertisements")

    target = asyncio.run(gatt_client.EasyChangeGattClient()._resolve_target(RejectScan))
    assert isinstance(target, FakeBLEDevice)
    assert target.address == "A1:B2:C3:D4:E5:F6"
    assert devices["esp"].closed is True


def test_multiple_same_named_paired_devices_fail_closed(monkeypatch):
    _fake_winrt_pair(monkeypatch, entries=[
        ("BT KeyboardMouse E", "one", 0x111111111111),
        ("BT KeyboardMouse E", "two", 0x222222222222),
    ])
    monkeypatch.delenv("EASYCHANGE_BLE_DEVICE_ADDRESS", raising=False)
    with pytest.raises(RuntimeError, match="MULTIPLE_SAME_NAME"):
        asyncio.run(gatt_client._paired_windows_ble_target())


def test_windows_bluetooth_override_must_be_exact_address(monkeypatch):
    _fake_winrt_pair(monkeypatch, entries=[])
    monkeypatch.setenv("EASYCHANGE_BLE_DEVICE_ADDRESS", "random-name")
    with pytest.raises(ValueError, match="ADDRESS_INVALID"):
        asyncio.run(gatt_client.EasyChangeGattClient()._resolve_target(Mock()))
    monkeypatch.setenv("EASYCHANGE_BLE_DEVICE_ADDRESS", "AA:BB:CC:DD:EE:FF")
    target = asyncio.run(gatt_client.EasyChangeGattClient()._resolve_target(Mock()))
    assert target.address == "AA:BB:CC:DD:EE:FF"


def test_windows_gatt_client_uses_uncached_services_and_reports_authenticated_state(monkeypatch):
    _fake_winrt_pair(monkeypatch, entries=[])
    created = []

    class FakeClient:
        def __init__(self, target, **kwargs):
            created.append((target, kwargs))
            self.is_connected = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def start_notify(self, uuid, callback):
            assert uuid == gatt_client.HOST_TO_REMOTE_UUID
            assert callable(callback)
            bridge._stop.set()

    monkeypatch.setattr(sys.modules["bleak"], "BleakClient", FakeClient, raising=False)
    monkeypatch.setattr(sys.modules["bleak"], "BleakScanner", object, raising=False)
    bridge = gatt_client.EasyChangeGattClient()

    async def target(_scanner):
        return types.SimpleNamespace(address="A1:B2:C3:D4:E5:F6")

    monkeypatch.setattr(bridge, "_resolve_target", target)
    asyncio.run(bridge._run())
    assert created[0][1]["winrt"] == {"use_cached_services": False}
    assert bridge._last_error == ""
    assert bridge._last_stage == "CONECTADO"


def test_disconnection_does_not_overwrite_root_error(monkeypatch):
    _fake_winrt_pair(monkeypatch, entries=[])

    class FailingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("CHARACTERISTIC_NOT_FOUND")

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(sys.modules["bleak"], "BleakClient", FailingClient, raising=False)
    monkeypatch.setattr(sys.modules["bleak"], "BleakScanner", object, raising=False)
    bridge = gatt_client.EasyChangeGattClient()
    events = []

    async def target(_scanner):
        return types.SimpleNamespace(address="A1:B2:C3:D4:E5:F6")

    monkeypatch.setattr(bridge, "_resolve_target", target)
    def track(event):
        events.append(event)
        if "GATT desconectado" in event.get("message", ""):
            bridge._stop.set()
    monkeypatch.setattr(bridge, "_emit", track)
    asyncio.run(bridge._run())
    assert len(events) == 1
    assert "CONEXAO_BLE" in events[-1]["message"]
    assert "CHARACTERISTIC_NOT_FOUND" in events[-1]["message"]
    assert bridge.status()["last_error"] == "RuntimeError: CHARACTERISTIC_NOT_FOUND"
