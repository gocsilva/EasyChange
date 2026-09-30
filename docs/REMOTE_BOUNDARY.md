# Remote boundary

EasyChange on REMOTE_PC is a local GUI. The only B.M.O. input is physical ESP32-S3 HID; the only B.M.O. output is HDMI capture. Do not run a remote MCP server/client, expose an API, or use shared network/filesystem/clipboard/SSH channels for remote control.

The MCP tools run on MCP_HOST and operate the physical HID and HDMI devices. EasyChange itself is unaware of B.M.O. and has no remote control endpoint in this architecture.
