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
        ("Easy", "esp", 0xA1B2C3D4E5F6),
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
        ("Easy", "one", 0x111111111111),
        ("Easy", "two", 0x222222222222),
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


def test_winrt_selector_overload_error_retries_two_argument_version(monkeypatch):
    _devices, FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[
        ("Easy", "esp", 0xA1B2C3D4E5F6),
    ])
    enumeration = sys.modules["winrt.windows.devices.enumeration"].DeviceInformation
    calls = []

    async def selector_plus_properties(*args):
        calls.append(args)
        if len(args) != 2:
            raise TypeError("Invalid parameter count")
        assert args[0] == "PAIRED_LE_ONLY" and args[1] == []
        return [types.SimpleNamespace(name="Easy", id="esp")]
    monkeypatch.setattr(enumeration, "find_all_async", selector_plus_properties)
    target = asyncio.run(gatt_client._paired_windows_ble_target())
    assert isinstance(target, FakeBLEDevice)
    assert target.address == "A1:B2:C3:D4:E5:F6"
    assert [len(args) for args in calls] == [1, 2]


def test_winrt_all_overloads_fallback_enumerates_only_paired(monkeypatch):
    _devices, FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[
        ("Easy", "unpaired", 0x102030405060),
        ("Easy", "paired", 0xA1B2C3D4E5F6),
    ])
    enumeration = sys.modules["winrt.windows.devices.enumeration"].DeviceInformation
    calls = []

    async def only_unfiltered(*args):
        calls.append(args)
        if args:
            raise TypeError("Invalid parameter count")
        return [
            types.SimpleNamespace(name="Easy", id="unpaired",
                                  pairing=types.SimpleNamespace(is_paired=False)),
            types.SimpleNamespace(name="Easy", id="paired",
                                  pairing=types.SimpleNamespace(is_paired=True)),
        ]
    monkeypatch.setattr(enumeration, "find_all_async", only_unfiltered)
    target = asyncio.run(gatt_client._paired_windows_ble_target())
    assert isinstance(target, FakeBLEDevice)
    assert target.address == "A1:B2:C3:D4:E5:F6"
    assert [len(args) for args in calls] == [1, 2, 2, 0]


def test_winrt_unfiltered_does_not_accept_missing_pairing_information(monkeypatch):
    _devices, _FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[
        ("Easy", "not-proven-paired", 0xA1B2C3D4E5F6)
    ])
    enumeration = sys.modules["winrt.windows.devices.enumeration"].DeviceInformation

    async def only_unfiltered(*args):
        if args:
            raise TypeError("Invalid parameter count")
        return [types.SimpleNamespace(name="Easy",
                                      id="not-proven-paired")]
    monkeypatch.setattr(enumeration, "find_all_async", only_unfiltered)
    assert asyncio.run(gatt_client._paired_windows_ble_target()) is None


def test_winrt_selector_error_retries_default_selector_and_filters_unpaired(monkeypatch):
    _devices, FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[
        ("Easy", "paired", 0xA1B2C3D4E5F6),
    ])
    ble = sys.modules["winrt.windows.devices.bluetooth"].BluetoothLEDevice
    enumeration = sys.modules["winrt.windows.devices.enumeration"].DeviceInformation

    def rejects_paired(_paired):
        raise TypeError("Invalid parameter count")
    monkeypatch.setattr(ble, "get_device_selector_from_pairing_state", rejects_paired)
    monkeypatch.setattr(ble, "get_device_selector", lambda: "ALL_BLE", raising=False)

    async def all_ble(*args):
        assert args == ("ALL_BLE",)
        return [types.SimpleNamespace(name="Easy", id="paired",
                                      pairing=types.SimpleNamespace(is_paired=True))]
    monkeypatch.setattr(enumeration, "find_all_async", all_ble)
    target = asyncio.run(gatt_client._paired_windows_ble_target())
    assert isinstance(target, FakeBLEDevice)
    assert target.address == "A1:B2:C3:D4:E5:F6"


def test_unusable_winrt_overloads_fall_back_to_ble_scanner(monkeypatch):
    _fake_winrt_pair(monkeypatch, entries=[])
    monkeypatch.delenv("EASYCHANGE_BLE_DEVICE_ADDRESS", raising=False)
    enumeration = sys.modules["winrt.windows.devices.enumeration"].DeviceInformation

    async def all_fail(*_args):
        raise TypeError("Invalid parameter count")
    monkeypatch.setattr(enumeration, "find_all_async", all_fail)

    class Scanner:
        @staticmethod
        async def discover(timeout):
            assert timeout == 4
            return [types.SimpleNamespace(
                name="Easy", address="AA:BB:CC:DD:EE:FF")]
    target = asyncio.run(gatt_client.EasyChangeGattClient()._resolve_target(Scanner))
    assert target.address == "AA:BB:CC:DD:EE:FF"



def test_winrt_two_argument_iterable_conversion_error_tries_no_arg(monkeypatch):
    _devices, FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[
        ("Easy", "esp", 0xA1B2C3D4E5F6),
    ])
    enumeration = sys.modules["winrt.windows.devices.enumeration"].DeviceInformation
    calls = []

    async def overloads(*args):
        calls.append(args)
        if len(args) == 1:
            raise TypeError("Invalid parameter count")
        if len(args) == 2:
            raise TypeError("Cannot convert list to WinRT IIterable")
        return [types.SimpleNamespace(
            name="Easy", id="esp",
            pairing=types.SimpleNamespace(is_paired=True))]
    monkeypatch.setattr(enumeration, "find_all_async", overloads)
    target = asyncio.run(gatt_client._paired_windows_ble_target())
    assert isinstance(target, FakeBLEDevice)
    assert target.address == "A1:B2:C3:D4:E5:F6"
    assert [len(call) for call in calls] == [1, 2, 2, 0]



def test_winrt_paired_le_query_must_specify_association_endpoint(monkeypatch):
    devices, FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[
        ("Easy", "esp", 0xA1B2C3D4E5F6),
    ])
    module = sys.modules["winrt.windows.devices.enumeration"]
    module.DeviceInformationKind = types.SimpleNamespace(
        ASSOCIATION_ENDPOINT=5, DEVICE_INTERFACE=1)
    calls = []

    async def enumerate_endpoint(*args):
        calls.append(args)
        if len(args) != 3 or args[2] != 5:
            return []   # Windows default DeviceInterface would miss the bond
        return [types.SimpleNamespace(
            name="Easy", id="esp",
            pairing=types.SimpleNamespace(is_paired=True))]
    monkeypatch.setattr(module.DeviceInformation, "find_all_async", enumerate_endpoint)
    info = {}
    device = asyncio.run(gatt_client._paired_windows_ble_target(info))
    assert isinstance(device, FakeBLEDevice)
    assert device.address == "A1:B2:C3:D4:E5:F6"
    assert calls == [("PAIRED_LE_ONLY", [], 5)]
    assert info == {"paired_records": 1, "named_records": 1,
                    "matching_records": 1, "verified_le_objects": 1,
                    "verified_aep_addresses": 0, "source": "AEP_LIST"}
    assert devices["esp"].closed is True


def test_empty_paired_aep_and_advertisement_reports_counts(monkeypatch):
    _fake_winrt_pair(monkeypatch, entries=[])
    module = sys.modules["winrt.windows.devices.enumeration"]
    module.DeviceInformationKind = types.SimpleNamespace(
        ASSOCIATION_ENDPOINT=5)
    monkeypatch.delenv("EASYCHANGE_BLE_DEVICE_ADDRESS", raising=False)

    class NoAdvertisements:
        @staticmethod
        async def discover(timeout):
            assert timeout == 4
            return []
    with pytest.raises(RuntimeError, match=r"records=0, named=0, verified=0, le=0, aep=0, source=SELECTOR_ONLY, scan=0"):
        asyncio.run(gatt_client.EasyChangeGattClient()._resolve_target(NoAdvertisements))


def test_named_unpaired_aep_cannot_be_selected(monkeypatch):
    _fake_winrt_pair(monkeypatch, entries=[
        ("Easy", "esp", 0xA1B2C3D4E5F6),
    ])
    module = sys.modules["winrt.windows.devices.enumeration"]
    module.DeviceInformationKind = types.SimpleNamespace(
        ASSOCIATION_ENDPOINT=5)

    async def enumerate_aep(*args):
        assert args[2] == 5
        return [types.SimpleNamespace(
            name="Easy", id="esp",
            pairing=types.SimpleNamespace(is_paired=False))]
    monkeypatch.setattr(module.DeviceInformation, "find_all_async", enumerate_aep)
    counts = {}
    device = asyncio.run(gatt_client._paired_windows_ble_target(counts))
    assert device is None
    assert counts["named_records"] == 1
    assert counts["matching_records"] == 0



def test_aep_nullable_winrt_iiiterable_succeeds_if_python_list_cannot_convert(monkeypatch):
    """Real PyWinRT bindings may reject [] for nullable IIterable[str]."""
    _devices, FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[
        ("Easy", "esp", 0xA1B2C3D4E5F6),
    ])
    module = sys.modules["winrt.windows.devices.enumeration"]
    module.DeviceInformationKind = types.SimpleNamespace(ASSOCIATION_ENDPOINT=5)
    calls = []
    async def find_all(*args):
        calls.append(args)
        if len(args) == 3 and args[1] is None and args[2] == 5:
            return [types.SimpleNamespace(
                name="Easy", id="esp",
                pairing=types.SimpleNamespace(is_paired=True))]
        raise TypeError("Invalid parameter count or invalid iterable type")
    monkeypatch.setattr(module.DeviceInformation, "find_all_async", find_all)
    seen = {}
    actual = asyncio.run(gatt_client._paired_windows_ble_target(seen))
    assert isinstance(actual, FakeBLEDevice)
    assert actual.address == "A1:B2:C3:D4:E5:F6"
    assert seen["source"] == "AEP_NULL"
    assert [len(row) for row in calls] == [3, 3]


def test_windows_child_interface_unpaired_but_verified_main_device_paired(monkeypatch):
    devices, FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[
        ("Easy", "child", 0xA1B2C3D4E5F6),
    ])
    module = sys.modules["winrt.windows.devices.enumeration"]
    async def unfiltered(*args):
        if args:
            raise TypeError("Invalid parameter count")
        return [types.SimpleNamespace(
            name="Easy", id="child",
            pairing=types.SimpleNamespace(is_paired=False))]
    monkeypatch.setattr(module.DeviceInformation, "find_all_async", unfiltered)
    devices["child"].device_information = types.SimpleNamespace(
        pairing=types.SimpleNamespace(is_paired=True))
    diag = {}
    actual = asyncio.run(gatt_client._paired_windows_ble_target(diag))
    assert isinstance(actual, FakeBLEDevice)
    assert actual.address == "A1:B2:C3:D4:E5:F6"
    assert diag["source"] == "UNFILTERED"
    assert diag["named_records"] == 1
    assert diag["matching_records"] == 1


def test_unpaired_main_device_cannot_use_paired_looking_child(monkeypatch):
    devices, _FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[
        ("Easy", "child", 0xA1B2C3D4E5F6),
    ])
    module = sys.modules["winrt.windows.devices.enumeration"]
    async def filtered(*args):
        return [types.SimpleNamespace(
            name="Easy", id="child",
            pairing=types.SimpleNamespace(is_paired=True))]
    monkeypatch.setattr(module.DeviceInformation, "find_all_async", filtered)
    devices["child"].device_information = types.SimpleNamespace(
        pairing=types.SimpleNamespace(is_paired=False))
    assert asyncio.run(gatt_client._paired_windows_ble_target()) is None


def test_bad_child_interface_ids_are_skipped_without_guessing_mac(monkeypatch):
    _devices, _FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[])
    module = sys.modules["winrt.windows.devices.enumeration"]
    ble_class = sys.modules["winrt.windows.devices.bluetooth"].BluetoothLEDevice
    async def all_rows(*args):
        return [types.SimpleNamespace(
            name="Easy", id="not-a-ble-aep",
            pairing=types.SimpleNamespace(is_paired=False))]
    async def bad_child_id(_device_id):
        raise TypeError("Invalid parameter count")
    monkeypatch.setattr(module.DeviceInformation, "find_all_async", all_rows)
    monkeypatch.setattr(ble_class, "from_id_async", bad_child_id)
    diag = {}
    assert asyncio.run(gatt_client._paired_windows_ble_target(diag)) is None
    assert diag["matching_records"] == 0



def test_winrt_paired_aep_address_avoids_ui_thread_only_fromid(monkeypatch):
    _devices, FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[])
    module = sys.modules["winrt.windows.devices.enumeration"]
    module.DeviceInformationKind = types.SimpleNamespace(ASSOCIATION_ENDPOINT=5)
    bclass = sys.modules["winrt.windows.devices.bluetooth"].BluetoothLEDevice

    async def find_aep(*args):
        if len(args) != 3:
            return []
        return [types.SimpleNamespace(
            name="Easy", id="aep-paired",
            pairing=types.SimpleNamespace(is_paired=True),
            properties={"System.Devices.Aep.DeviceAddress": "a1:b2:c3:d4:e5:f6"})]
    async def forbid_second_winrt_lookup(_id):
        raise AssertionError("An authenticated AEP already has a verified address")
    monkeypatch.setattr(module.DeviceInformation, "find_all_async", find_aep)
    monkeypatch.setattr(bclass, "from_id_async", forbid_second_winrt_lookup)
    info = {}
    target = asyncio.run(gatt_client._paired_windows_ble_target(info))
    assert isinstance(target, FakeBLEDevice)
    assert target.address == "A1:B2:C3:D4:E5:F6"
    assert info["verified_aep_addresses"] == 1
    assert info["verified_le_objects"] == 0
    assert info["matching_records"] == 1


def test_unpaired_aep_cannot_supply_address_even_with_valid_mac(monkeypatch):
    _devices, _FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[])
    module = sys.modules["winrt.windows.devices.enumeration"]
    module.DeviceInformationKind = types.SimpleNamespace(ASSOCIATION_ENDPOINT=5)
    bclass = sys.modules["winrt.windows.devices.bluetooth"].BluetoothLEDevice

    async def find_aep(*args):
        return [types.SimpleNamespace(
            name="Easy", id="aep-unpaired",
            pairing=types.SimpleNamespace(is_paired=False),
            properties={"System.Devices.Aep.DeviceAddress": "a1:b2:c3:d4:e5:f6"})]
    async def missing_le(_id):
        return None
    monkeypatch.setattr(module.DeviceInformation, "find_all_async", find_aep)
    monkeypatch.setattr(bclass, "from_id_async", missing_le)
    assert asyncio.run(gatt_client._paired_windows_ble_target()) is None


def test_paired_aep_invalid_address_requires_independent_verification(monkeypatch):
    _devices, _FakeBLEDevice = _fake_winrt_pair(monkeypatch, entries=[])
    module = sys.modules["winrt.windows.devices.enumeration"]
    module.DeviceInformationKind = types.SimpleNamespace(ASSOCIATION_ENDPOINT=5)
    bclass = sys.modules["winrt.windows.devices.bluetooth"].BluetoothLEDevice

    async def find_aep(*args):
        return [types.SimpleNamespace(
            name="Easy", id="aep-bad-mac",
            pairing=types.SimpleNamespace(is_paired=True),
            properties={"System.Devices.Aep.DeviceAddress": "this-is-not-a-mac"})]
    async def missing_le(_id):
        return None
    monkeypatch.setattr(module.DeviceInformation, "find_all_async", find_aep)
    monkeypatch.setattr(bclass, "from_id_async", missing_le)
    assert asyncio.run(gatt_client._paired_windows_ble_target()) is None



def test_gatt_notify_inventory_reports_actual_service_characteristic_cccd():
    import types
    cccd = types.SimpleNamespace(uuid="00002902-0000-1000-8000-00805f9b34fb")
    notify = types.SimpleNamespace(handle=79, properties=["notify"],
                                   descriptors=[cccd])
    class Services:
        def get_service(self, uuid):
            assert uuid == gatt_client.SERVICE_UUID
            return object()
        def get_characteristic(self, uuid):
            assert uuid == gatt_client.HOST_TO_REMOTE_UUID
            return notify
    details = gatt_client._gatt_notify_inventory(Services())
    assert details == "service=1,notify_char=1,notify=1,cccd=1,handle=79"


def test_gatt_notify_inventory_flags_missing_cccd_and_notify_without_writes():
    import types
    class Services:
        def get_service(self, uuid):
            return object()
        def get_characteristic(self, uuid):
            return types.SimpleNamespace(handle=3, properties=["read"],
                                         descriptors=[])
    result = gatt_client._gatt_notify_inventory(Services())
    assert result == "service=1,notify_char=1,notify=0,cccd=0,handle=3"
    assert gatt_client._gatt_notify_inventory(None) == "gatt=unavailable"


def test_gatt_notify_failure_preserves_inventory_in_final_error(monkeypatch):
    import types
    _fake_winrt_pair(monkeypatch, entries=[])
    class Services:
        def get_service(self, uuid):
            return object()
        def get_characteristic(self, uuid):
            return types.SimpleNamespace(handle=24, properties=["notify"],
                descriptors=[types.SimpleNamespace(uuid="00002902-0000-1000-8000-00805f9b34fb")])
    class FailingClient:
        is_connected = True
        services = Services()
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def start_notify(self, uuid, callback):
            raise RuntimeError("GATT Protocol Error Write Not Permitted (0x03)")
    monkeypatch.setattr(sys.modules["bleak"], "BleakClient", FailingClient, raising=False)
    monkeypatch.setattr(sys.modules["bleak"], "BleakScanner", object, raising=False)
    bridge = gatt_client.EasyChangeGattClient()
    async def target(_):
        return types.SimpleNamespace(address="A1:B2:C3:D4:E5:F6")
    monkeypatch.setattr(bridge, "_resolve_target", target)
    events = []
    def record(e):
        events.append(e)
        if "desconectado" in str(e.get("message")):
            bridge._stop.set()
    monkeypatch.setattr(bridge, "_emit", record)
    asyncio.run(bridge._run())
    msg = events[-1]["message"]
    assert "Write Not Permitted" in msg
    assert "INSCRICAO_GATT" in msg
    assert "notify_char=1,notify=1,cccd=1,handle=24" in msg
    assert bridge.status()["secure"] is False


def test_att03_classifier_distinguishes_write_permission_from_security_and_other_errors():
    class BleakProtocol:
        protocol_error = 3
        def __str__(self):
            return "BLE GATT Protocol Error: Write Not Permitted"
    class SecurityFailure:
        protocol_error = 5
        def __str__(self):
            return "GATT: Insufficient Authentication"
    assert gatt_client._is_cccd_write_denied(BleakProtocol()) is True
    assert gatt_client._is_cccd_write_denied(SecurityFailure()) is False
    assert gatt_client._is_cccd_write_denied(
        RuntimeError("GATT Protocol Error: Write Not Permitted")) is True
    assert gatt_client._is_cccd_write_denied(
        RuntimeError("Insufficient Encryption")) is False
    assert gatt_client._is_cccd_write_denied(
        RuntimeError("GATT write failed with timeout")) is False


def test_cccd_probe_is_readonly_and_uses_fresh_descriptor_read():
    cccd = types.SimpleNamespace(uuid="00002902-0000-1000-8000-00805f9b34fb")
    ch = types.SimpleNamespace(descriptors=[cccd])
    class Services:
        def get_characteristic(self, uuid):
            assert uuid == gatt_client.HOST_TO_REMOTE_UUID
            return ch
    class Client:
        services = Services()
        async def read_gatt_descriptor(self, descriptor, *, use_cached):
            assert descriptor is cccd
            assert use_cached is False
            return b"\x00\x00"
        async def write_gatt_descriptor(self, *_args, **_kwargs):
            raise AssertionError("CCCD must never be written directly")
    assert asyncio.run(gatt_client._cccd_readonly_probe(Client())) == "CCCD_READ_OK:0x0000"


def test_cccd_probe_failure_does_not_hide_root_att03_error():
    cccd = types.SimpleNamespace(uuid="00002902-0000-1000-8000-00805f9b34fb")
    class Client:
        services = types.SimpleNamespace(
            get_characteristic=lambda uuid: types.SimpleNamespace(descriptors=[cccd]))
        async def read_gatt_descriptor(self, *_args, **_kwargs):
            raise PermissionError("read permission denied")
    status = asyncio.run(gatt_client._cccd_readonly_probe(Client()))
    assert status.startswith("CCCD_READ_FAILED:PermissionError")


def test_repeated_att03_is_bounded_and_never_marks_session_authenticated(monkeypatch):
    _fake_winrt_pair(monkeypatch, entries=[])
    count = []
    class FailingClient:
        is_connected = True
        services = None
        def __init__(self, *_args, **_kwargs):
            count.append("attempt")
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return None
        async def start_notify(self, *_args):
            raise RuntimeError("GATT Protocol Error: Write Not Permitted")
    monkeypatch.setattr(sys.modules["bleak"], "BleakClient", FailingClient, raising=False)
    monkeypatch.setattr(sys.modules["bleak"], "BleakScanner", object, raising=False)
    bridge = gatt_client.EasyChangeGattClient()
    events = []
    delay_requests = []
    async def resolve(_):
        return types.SimpleNamespace(address="BLEDevice")
    async def short_circuit_delay(seconds):
        delay_requests.append(seconds)
        if seconds is None:
            bridge._stop.set()
    monkeypatch.setattr(bridge, "_resolve_target", resolve)
    monkeypatch.setattr(bridge, "_retry_delay", short_circuit_delay)
    monkeypatch.setattr(bridge, "_emit", lambda item: events.append(item))
    asyncio.run(bridge._run())
    assert len(count) == 2
    assert delay_requests == [3, None]
    assert any("duas recusas ATT 0x03" in
               event.get("message", "") for event in events)
    assert bridge._accepted is False
    assert bridge._consecutive_cccd_denials == 0
    assert any(row["stage"] == "CCCD_ATT03_ACTION_REQUIRED"
               for row in bridge.diagnostics.snapshot()["events"])


def test_manual_retry_is_only_a_worker_wakeup_not_windows_policy_change():
    bridge = gatt_client.EasyChangeGattClient()
    assert bridge.retry_now() is False
    class FakeLoop:
        def is_running(self):
            return True
        def call_soon_threadsafe(self, callback):
            callback()
    class FakeSignal:
        called = 0
        def set(self):
            self.called += 1
    signal = FakeSignal()
    bridge._loop = FakeLoop()
    bridge._retry_wakeup = signal
    assert bridge.retry_now() is True
    assert signal.called == 1



def test_att03_cccd_read_runs_while_ble_link_is_still_connected(monkeypatch):
    """Regression: async-with exit disconnects before an outer except block."""
    _fake_winrt_pair(monkeypatch, entries=[])
    order = []
    cccd = types.SimpleNamespace(
        uuid="00002902-0000-1000-8000-00805f9b34fb", handle=113)

    class Services:
        def get_service(self, uuid):
            return types.SimpleNamespace()
        def get_characteristic(self, uuid):
            return types.SimpleNamespace(
                descriptors=[cccd], handle=111, properties=["notify"])

    class FailingClient:
        services = Services()
        is_connected = True

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            order.append("connect")
            return self

        async def __aexit__(self, *args):
            order.append("disconnect")
            self.is_connected = False

        async def start_notify(self, uuid, callback):
            order.append("notify")
            raise RuntimeError("GATT Protocol Error: Write Not Permitted (0x03)")

        async def read_gatt_descriptor(self, descriptor, *, use_cached):
            assert self.is_connected, "Must read CCCD before client.__aexit__"
            assert descriptor is cccd and use_cached is False
            order.append("cccd_read")
            return b"\x00\x00"

    monkeypatch.setattr(sys.modules["bleak"], "BleakClient", FailingClient, raising=False)
    monkeypatch.setattr(sys.modules["bleak"], "BleakScanner", object, raising=False)
    bridge = gatt_client.EasyChangeGattClient()

    async def device(_scanner):
        return types.SimpleNamespace(address="AA:BB:CC:DD:EE:FF")

    async def end_round(seconds):
        bridge._stop.set()

    monkeypatch.setattr(bridge, "_resolve_target", device)
    monkeypatch.setattr(bridge, "_retry_delay", end_round)
    asyncio.run(bridge._run())
    assert order == ["connect", "notify", "cccd_read", "disconnect"]
    events = bridge.diagnostics.snapshot(0, 200)["events"]
    probe = [event["message"] for event in events
             if event["stage"] == "CCCD_ATT03_PROBE"]
    assert len(probe) == 1
    assert "CCCD_READ_OK:0x0000" in probe[0]
    assert "conectado=True" in probe[0]
    assert bridge._accepted is False


def test_custom_gatt_probe_is_read_only_without_cccd_or_secret_payload():
    from easychange.remote import gatt_client
    import types
    characteristic = types.SimpleNamespace(properties=["read"], handle=116)
    class Services:
        def get_characteristic(self, uuid):
            assert uuid == gatt_client.DIAGNOSTIC_READ_UUID
            return characteristic
    class Client:
        services = Services()
        async def read_gatt_char(self, specifier, *, use_cached):
            assert specifier is characteristic and use_cached is False
            return b"BMO-GATT-DIAG-1"
        async def write_gatt_char(self, *_args, **_kwargs):
            raise AssertionError("Read-only diagnostic must never write")
        async def start_notify(self, *_args, **_kwargs):
            raise AssertionError("Probe must not subscribe to notifications")
    assert asyncio.run(gatt_client._custom_service_read_probe(Client())) == "DIAG_READ_OK"


def test_custom_gatt_probe_reports_old_firmware_or_access_denied():
    from easychange.remote import gatt_client
    import types
    class OldClient:
        services = types.SimpleNamespace(get_characteristic=lambda _: None)
    assert asyncio.run(gatt_client._custom_service_read_probe(OldClient())) == "DIAG_CHARACTERISTIC_ABSENT"
    class ReadDenied:
        services = types.SimpleNamespace(get_characteristic=lambda _: types.SimpleNamespace(
            properties=["read"]))
        async def read_gatt_char(self, *_args, **_kwargs):
            raise PermissionError("Access Denied")
    result = asyncio.run(gatt_client._custom_service_read_probe(ReadDenied()))
    assert result == "DIAG_READ_FAILED:PermissionError:Access Denied"
