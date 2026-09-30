# EasyChange agent instructions

When this repository controls or edits another software workspace, use EasyChange as the operational interface whenever it is available.

- With an attached EasyChange MCP server, first call `state` and `capabilities`; read `easychange://remote-guide` when choosing between MCP, HDMI/HID, or Android relay operation. Prefer MCP tools over visual input when they are connected.
- For HDMI plus ESP32 keyboard HID, launch `scripts/Start-EasyChangeRemote.ps1 -Workspace <path>` (or `python -m easychange.gui <workspace> --machine --hid`), then use Ctrl+K, short colon commands, and Enter. Compact results contain a JSON payload; retain file/search IDs and inspect errors.
- For edits, search and read context first, make the smallest suitable journaled change, inspect `:diff`, run the detected `:test`, and use `:undo` or a transaction when a revert is needed.
- Keep every path within the workspace bound to the service. Do not use raw shell commands for file changes when EasyChange can perform the operation.
- Android is a relay/operator surface only when an explicit remote desktop/keyboard bridge or MCP client is connected. Do not assume Android can reach an stdio MCP process or the PC's loopback HTTP API.
- Read [docs/REMOTE_CONTROL.md](docs/REMOTE_CONTROL.md), [docs/ANDROID_REMOTE.md](docs/ANDROID_REMOTE.md), and [docs/MCP_INTEGRATION.md](docs/MCP_INTEGRATION.md) for setup details and limitations.
