# EasyChange GATT on Windows after the ESP32-S3 firmware update

The original ESP32 HID Bluetooth name is `BT KeyboardMouse E` and the new
encrypted data service is attached to the **same** ESP and Bluetooth device.
USB COM4 stays physically connected to the MCP_HOST controller, not REMOTE_PC.
The GATT client runs in the existing remote EasyChange Qt GUI and is not a
second MCP server.

After flashing firmware 3.0.0, the Windows HID bond might remain connected
even if the firmware's new custom GATT service is not in the Windows cached
attribute database. Because connected HID devices may stop advertising, a
regular `BleakScanner.discover()` scan might also return nothing.

The remote EasyChange GATT client now:

- Resolves the existing *paired Bluetooth LE* device through Windows WinRT
  pairing enumeration, checking the exact device name before connecting;
  refuses multiple addresses with the same name instead of choosing one.
- Uses an existing Bleak BLEDevice reference rather than triggering an
  implicit second BLE advertisement scan.
- Requests `winrt={"use_cached_services": False}` for fresh GATT services,
  characteristics and descriptors. This does not unpair the HID device.
- Never overwrites a BLE/GATT failure with a generic `GATT desconectado`
  immediately afterwards. The Qt label retains the specific failure and the
  stage (`BUSCA_BLE`, `CONEXAO_BLE`, `INSCRICAO_GATT`,
  `CONECTADO`), with word wrap and copyable diagnostic text.
- Keeps QR and encrypted application SAS pairing policies unchanged. Bluetooth
  pairing alone is never considered authentication for EasyChange commands.

If the remote EasyChange still reports a missing GATT characteristic or
service after updating, remove **only** `BT KeyboardMouse E` from the
Windows Bluetooth device list, pair it again (to refresh the Windows bond and
service cache), reopen EasyChange, and retry pairing via the local MCP GUI.
This will briefly interrupt the HID/QR transport. No ESP firmware flash and
no Windows administrator permission are required for this application-level
troubleshooting. If scanning and paired enumeration fail, you may configure
`EASYCHANGE_BLE_DEVICE_ADDRESS` with the **actual** Bluetooth LE MAC
address for this ESP and reopen EasyChange. Never guess an address.

Follow status displayed on the remote EasyChange:

- `BUSCA_BLE`: device pairing/discovery problem.
- `CONEXAO_BLE`: WinRT connection, device reachability or service database.
- `INSCRICAO_GATT`: subscribe to custom notification characteristic failed;
  common when Windows still holds a stale GATT cache.
- `CONECTADO`: physical GATT link was opened; only successful SAS comparison
  and authenticated receipt can make it secure and ready for execution.


### Invalid parameter count during BUSCA_BLE (WinRT)

Some WinRT Python projections reject the one-filter
`DeviceInformation.find_all_async(aqs_filter)` overload with
`TypeError: Invalid parameter count`. The client now retries the two-argument
WinRT signature before resorting to an unfiltered enumeration that must prove
`DeviceInformation.pairing.is_paired`. It then falls back to the normal BLE
scanner when paired-device enumeration itself cannot run. No unpaired item
from an unfiltered enumeration may be used for GATT. These alternatives are
read-only OS discovery, and don't unpair HID, flash the ESP, or bypass SAS.


### Paired device connected as HID but absent from discovery

Windows Bluetooth paired-device AQS selectors identify
`DeviceInformationKind.ASSOCIATION_ENDPOINT`, not the default device
interface kind. The EasyChange client now requests the association endpoint
explicitly via the three-argument WinRT enumeration API, falling back only
when the binding lacks that overload.

If no device was discovered, the displayed BUSCA_BLE error includes counts
`paired` (Windows paired BLE association records), `named` (records
matching our ESP HID device name), `matched` (records with verified pairing
status), and `scan` (raw advertisements seen by Bleak). These counters do
not identify a device by name alone or bypass secure SAS verification.
