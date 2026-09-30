# Android remote operation

This guide is the Android handoff profile for EasyChange. It describes how an Android operator/agent can drive a remote Windows PC without assuming that the phone itself has direct hardware access.

## If Android has a remote desktop and keyboard bridge

1. Connect to the Windows desktop using the remote-control app already configured for that PC.
2. Open EasyChange on the PC with `scripts/Start-EasyChangeRemote.ps1 -Workspace 'C:\path\to\project'`.
3. Use the remote keyboard's **Ctrl+K** to focus the command field. The GUI shows a `:remote` boot card; use `:remote-guide` for instructions or `:prepare-hid` to switch modes. Then run `:locate <term> --context 2` and edit with `:edit-result <R-id> <replacement line>`. Press Enter after each command.
4. Read the compact JSON result payload on screen; copy the returned path, line, result ID, and error code exactly. Validate with `:diff && :test`.

## EasyChange on REMOTE_PC

Do not run/connect an EasyChange MCP server/client or HTTP API on REMOTE_PC for B.M.O. control. Android video is output only; remote input is the physical ESP32 HID and screen output is HDMI. Optional EasyChange MCP/HTTP adapters are for workspaces local to MCP_HOST only.

## If Android is only a chat client

A chat app alone cannot see or type on the Windows PC. EasyChange does not pair Android apps or provision network access. Use the established B.M.O. HDMI+ESP32 path for REMOTE_PC work.

## Transport and safety

The optional HTTP API is loopback-only and for MCP_HOST-local workspaces; never expose it to Android or REMOTE_PC. Use `:diff`, `:test`, and journaled undo/transactions to review changes.
