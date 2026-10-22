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
