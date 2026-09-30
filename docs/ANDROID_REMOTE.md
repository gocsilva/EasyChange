# Android remote operation

This guide is the Android handoff profile for EasyChange. It describes how an Android operator/agent can drive a remote Windows PC without assuming that the phone itself has direct hardware access.

## If Android has a remote desktop and keyboard bridge

1. Connect to the Windows desktop using the remote-control app already configured for that PC.
2. Open EasyChange on the PC with `scripts/Start-EasyChangeRemote.ps1 -Workspace 'C:\path\to\project'`.
3. Use the remote keyboard's **Ctrl+K** to focus the command field. The GUI shows a `:remote` boot card; use `:remote-guide` for instructions or `:prepare-hid` to switch modes. Then run `:locate <term> --context 2` and edit with `:edit-result <R-id> <replacement line>`. Press Enter after each command.
4. Read the compact JSON result payload on screen; copy the returned path, line, result ID, and error code exactly. Validate with `:diff && :test`.

## If Android has an MCP client

Connect the MCP client to an EasyChange MCP server running on the same Windows host as the workspace, then read `easychange://remote-guide` and call `state` and `capabilities`. The built-in server uses stdio, so it must be launched by an MCP host on that Windows computer; an Android device cannot connect to stdio over the network.

## If Android is only a chat client

A chat app alone cannot see or type on the Windows PC. It needs an already configured remote desktop/keyboard bridge, an MCP host on the PC, or the B.M.O. HDMI+ESP32 path. EasyChange does not pair Android apps or provision network access.

## Transport and safety

The default HTTP API listens on loopback and has no network authentication. Do not expose it to Android over Wi-Fi by changing the bind address. Keep remote operations on the existing trusted control bridge. Use `:diff`, `:test`, and journaled undo/transactions to review changes.
