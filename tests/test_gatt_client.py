import queue
from easychange.remote.gatt_client import EasyChangeGattClient

def test_client_does_not_connect_or_accept_commands_implicitly():
    bridge = EasyChangeGattClient()
    assert bridge.status()["secure"] is False
    assert bridge.commands.empty()
    assert bridge._session is None

def test_response_without_pairing_not_published():
    bridge = EasyChangeGattClient()
    bridge.publish_result("QABCDEF", {"ok":True})
    assert bridge.commands.empty()
