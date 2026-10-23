import pytest

from easychange.remote.transport_router import TransportRouter, TransportUnavailable


def test_auto_keeps_existing_optical_without_verified_gatt():
    router = TransportRouter()
    assert router.select(now=10).transport == "OPTICAL"
    router.gatt_heartbeat(authenticated=True, session_id="s1", now=10)
    assert router.select(now=11).transport == "GATT"
    assert router.select(now=16).transport == "OPTICAL"


def test_gatt_never_runs_without_authentication():
    router = TransportRouter("GATT")
    with pytest.raises(TransportUnavailable, match="AUTHENTICATED"):
        router.select(now=10)
    router.gatt_heartbeat(authenticated=False, session_id="s1", now=10)
    with pytest.raises(TransportUnavailable):
        router.select(now=10)


def test_explicit_optical_and_user_can_work_background():
    router = TransportRouter("OPTICAL")
    router.gatt_heartbeat(authenticated=True, session_id="s1", now=10)
    assert router.select(now=11).background_safe is False
    router.set_mode("AUTO")
    assert router.select(now=11).background_safe is True


def test_auto_faster_measured_delivery_selection():
    router = TransportRouter()
    router.gatt_heartbeat(authenticated=True, session_id="s1", now=10)
    for _ in range(3):
        router.record_delivery("GATT", 300)
        router.record_delivery("OPTICAL", 100)
    assert router.select(now=10).transport == "OPTICAL"


def test_never_new_transport_after_mutation_started():
    router = TransportRouter()
    with pytest.raises(TransportUnavailable, match="RECONCILE"):
        router.select(now=10, in_flight_mutation=True)


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        TransportRouter("UNKNOWN")

@pytest.mark.parametrize("invalid_session", [None, "", "  \t", 0, b"sid", []])
def test_invalid_gatt_session_never_enables_transport(invalid_session):
    router = TransportRouter("AUTO")
    router.gatt_heartbeat(authenticated=True, session_id=invalid_session, now=10)
    assert router.select(now=11).transport == "OPTICAL"
    router.set_mode("GATT")
    with pytest.raises(TransportUnavailable, match="AUTHENTICATED"):
        router.select(now=11)


def test_gatt_requires_boolean_authentication_even_when_session_looks_valid():
    router = TransportRouter("AUTO")
    router.gatt_heartbeat(authenticated="false", session_id="s1", now=10)
    assert router.select(now=11).transport == "OPTICAL"
