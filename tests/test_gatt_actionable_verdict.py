"""GATT log must distinguish confirmed causes from ambiguous errors."""
from easychange.remote.gatt_client import _gatt_verdict


def verdict(access, read, cccd, error, metadata="service=1,notify_char=1,notify=1,cccd=1"):
    return _gatt_verdict(stage="INSCRICAO_GATT", winrt_status=access,
                         read_probe=read, cccd_probe=cccd,
                         notification_error=error, metadata=metadata,
                         connected=True)


def test_canonical_double_denial_does_not_misattribute_windows_or_esp_firmware():
    value=verdict("WINRT_ACCESS_INFORMATION_UNAVAILABLE",
                  "DIAG_READ_FAILED:BleakError:Access Denied",
                  "CCCD_READ_FAILED:BleakError:Access Denied",
                  "BleakGATTProtocolError: GATT Protocol Error: Write Not Permitted (3)")
    assert value["codigo"] == "ACCESS_DENIED_READ_AND_ATT03_CCCD"
    assert value["certeza"] == "CAUSA_AINDA_NAO_ISOLADA"
    assert "BLE_DIAG" in value["proximo_teste"]
    assert "ATT_0X03" in value["notificacao"]


def test_explicit_winrt_access_denial_is_confirmed():
    value=verdict("WINRT_ACCESS_STATUS=DENIED_BY_SYSTEM", "not", "not", "ATT 0x03")
    assert value["codigo"]=="WINRT_SERVICE_ACCESS_DENIED"
    assert value["certeza"]=="CONFIRMADO_PELO_WINDOWS"


def test_missing_cccd_is_confirmed_inventory_failure():
    value=verdict("WINRT_ACCESS_STATUS=ALLOWED", "not", "not", "ATT 0x03",
                  metadata="service=1,notify_char=1,notify=1,cccd=0")
    assert value["codigo"] == "GATT_SCHEMA_MISSING_OR_STALE"


def test_client_read_without_cccd_error():
    value=verdict("WINRT_ACCESS_STATUS=ALLOWED", "DIAG_READ_OK",
                  "NOT_NEEDED", "")
    assert value["codigo"]=="CUSTOM_GATT_READ_CONFIRMED"
    assert value["leitura"]=="DIAG_READ_OK"


def test_successful_subscription_is_not_reported_as_a_gatt_failure():
    for read in ("DIAG_READ_OK", "DIAG_CHARACTERISTIC_ABSENT",
                 "DIAG_READ_FAILED:BleakError:Access Denied"):
        value = _gatt_verdict(stage="NOTIFY_OK", winrt_status="WINRT_ACCESS_STATUS=ALLOWED",
                              read_probe=read, cccd_probe="NOT_NEEDED",
                              notification_error="", metadata="service=1,notify_char=1,cccd=1",
                              connected=True)
        assert value["codigo"] == "GATT_SUBSCRIPTION_CONFIRMED"
        assert "SAS/AEAD" in value["proximo_teste"]
        assert value["leitura"] == read
