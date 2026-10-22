"""WinRT must use ESP32 Easy's actual static RANDOM BLE address type."""
from pathlib import Path

SOURCE=(Path(__file__).resolve().parents[1] /
        "easychange/remote/gatt_client.py").read_text(encoding="utf-8")


def test_easy_ble_address_type_is_explicit_and_non_cached():
    assert '"address_type": "random"' in SOURCE
    assert '"use_cached_services": False' in SOURCE
    assert 'tipo_endereco=' in SOURCE
    assert 'BLE_ADDR_TYPE_RANDOM' in SOURCE


def test_no_windows_pairing_or_unpairing_changes():
    assert "await client.pair(" not in SOURCE
    assert "await client.unpair(" not in SOURCE
    assert "request_access_async()" not in SOURCE
