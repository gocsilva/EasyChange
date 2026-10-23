# Remote boundary

EasyChange on REMOTE_PC is a native local GUI. **No remote MCP server/client, TCP listener, HTTP/WebSocket API, SSH, network file access or shared clipboard** is allowed.

Original mode `QR` is preserved: commands are typed by physical ESP32-S3 USB/BLE HID and results are read by MCP_HOST through HDMI/QR.

An explicitly owner-approved alternative uses the same paired ESP32-S3 as a BLE GATT server. EasyChange starts only a **Windows GATT client** that subscribes to ESP notifications and writes encrypted responses to the ESP; the ESP routes opaque frames through the existing MCP_HOST COM4 serial cable. There is still **no MCP software or network service on REMOTE_PC**.

Only mutually verified X25519 sessions confirmed by human six-digit short-code comparison can execute application commands. Every command/result uses AES-256-GCM with sequence/replay validation. Commands are queued to the **existing CommandService** worker and returned without keyboard typing, mouse movement, UI focus changes or QR painting. HID/optical remains available unchanged as the fallback.

The MCP GUI `EasyChange` tab selects AUTO, QR or GATT at runtime; the remote never decides how other MCP tools are dispatched. User-triggered `Atualizar_EasyChange.bat` installs the `bleak` GUI extra. A device that stops advertising after BLE HID pairing may require setting `EASYCHANGE_BLE_DEVICE_ADDRESS` for the native client.


## ECP1 pixel optical mode (2026-10-22)

The canonical GUI now offers **AUTO / GATT / QR / PIXEL**, without removing
the manual **Ligar GATT** toggle. The default launch remains AUTO with GATT
disabled until an explicit operator action. QR is the original EC2 encoding;
PIXEL is a versioned 56x56 black/white pixel bitmap (ECP1) of the *same*
EC2/EC2H packet. The local MCP recognizes both in a single HDMI frame,
including mixed pages with an oversized QR packet.

MCP's explicit PIXEL preference is conveyed as
\`:ec <original-sequence> :view PIXEL <original-command>\`. Only the existing
Machine GUI strips \`:view\`, before CommandService receives exactly the
original sequenced EC1 request. This hint never creates a new command,
executes/retries a mutation, or bypasses the existing security and durable
EC2 checksum checks. An explicit **remote UI selector** overrides the hint;
when the remote selector is AUTO, the host hint controls PIXEL versus QR.
Every rendered packet carries \`tm\` (effective visual format) and \`rm\`
(manual remote selection) metadata, not part of the result data/hash.

ECP1 verifies its registration border, maximum length, magic bytes and
CRC32 before parsing EC2 JSON. Existing EC2 sequence/fragment CRC/SHA
validation remains mandatory. A packet larger than ECP1's 302-byte text
limit is still displayed as ordinary QR. The GUI never removes or changes
GATT and USB/BLE keyboard/mouse behavior. **No ESP firmware changes** and
no new remote software/service/network listener are required beyond
updating the existing EasyChange app.

For initial use, update both GitHub mains, choose **PIXEL** in EasyChange
or the local MCP tab, then verify physical HDMI capture and a correlated
\`EC2_PIXEL\` response. Do not infer live reliability/latency from simulated
JPEG tests. If the remote mode marker is not visible in the latest HDMI
frame, MCP retains the local configured preference; manual remote
selection takes precedence when recognized. Existing QR mode remains the
fallback and can be selected explicitly at any time.
