# Remote boundary

EasyChange on REMOTE_PC is a native local GUI. **No remote MCP server/client, TCP listener, HTTP/WebSocket API, SSH, network file access or shared clipboard** is allowed.

Original mode `QR` is preserved: commands are typed by physical ESP32-S3 USB/BLE HID and results are read by MCP_HOST through HDMI/QR.

An explicitly owner-approved alternative uses the same paired ESP32-S3 as a BLE GATT server. EasyChange starts only a **Windows GATT client** that subscribes to ESP notifications and writes encrypted responses to the ESP; the ESP routes opaque frames through the existing MCP_HOST COM4 serial cable. There is still **no MCP software or network service on REMOTE_PC**.

Only mutually verified X25519 sessions confirmed by human six-digit short-code comparison can execute application commands. Every command/result uses AES-256-GCM with sequence/replay validation. Commands are queued to the **existing CommandService** worker and returned without keyboard typing, mouse movement, UI focus changes or QR painting. HID/optical remains available unchanged as the fallback.

The MCP GUI `EasyChange` tab selects AUTO, QR or GATT at runtime; the remote never decides how other MCP tools are dispatched. User-triggered `Atualizar_EasyChange.bat` installs the `bleak` GUI extra. A device that stops advertising after BLE HID pairing may require setting `EASYCHANGE_BLE_DEVICE_ADDRESS` for the native client.
