"""Independent transport contract: neither endpoint executes on unapproved BLE packets."""
import pytest

try:
    from app.easychange_gatt_security import GattHandshake, GattSecurityError
except ImportError:
    from easychange.remote.gatt_security import GattHandshake, GattSecurityError


def pair():
    host, remote = GattHandshake("host"), GattHandshake("remote")
    a = host.receive_peer(remote.public_key)
    b = remote.receive_peer(host.public_key)
    assert a == b
    return host.approve(b), remote.approve(a)


def test_verified_encrypted_roundtrip():
    host, remote = pair()
    assert remote.decrypt(host.encrypt(b":ec C1 :state")) == b":ec C1 :state"
    assert host.decrypt(remote.encrypt(b"OK C1")) == b"OK C1"


def test_tampering_and_replay_rejected():
    host, remote = pair()
    packet = host.encrypt(b"source text")
    corrupted = bytearray(packet)
    corrupted[-1] ^= 1
    with pytest.raises(GattSecurityError, match="AUTHENTICATION_FAILED"):
        remote.decrypt(bytes(corrupted))
    assert remote.decrypt(packet) == b"source text"
    with pytest.raises(GattSecurityError, match="REPLAY_REJECTED"):
        remote.decrypt(packet)


def test_pairing_is_not_implicit():
    host, remote = GattHandshake("host"), GattHandshake("remote")
    with pytest.raises(GattSecurityError, match="HANDSHAKE_NOT_READY"):
        host.approve("000000")
    sas = host.receive_peer(remote.public_key)
    remote.receive_peer(host.public_key)
    with pytest.raises(GattSecurityError, match="PAIRING_PROOF_MISMATCH"):
        host.approve("999999" if sas != "999999" else "111111")
    assert host.approve(sas)
    with pytest.raises(GattSecurityError, match="HANDSHAKE_NOT_READY"):
        host.approve(sas)


def test_direction_bound():
    host, remote = pair()
    with pytest.raises(GattSecurityError, match="DIRECTION_MISMATCH"):
        host.decrypt(host.encrypt(b"same side"))
