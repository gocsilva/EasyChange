# Remote control and B.M.O.

For REMOTE_PC, EasyChange is a local GUI controlled only through the physical boundary: ESP32-S3 HID input and HDMI output. Do not run an MCP server/client, HTTP API, shared filesystem, clipboard, SSH, or other B.M.O. channel on that computer. Optional MCP/HTTP adapters are for MCP_HOST-local workspaces only. The GUI returns focus to its persistent command field after actions; **Ctrl+K** or **Esc** is recovery, and **F12** toggles high-contrast Machine Mode.

The GUI opens with a `:remote` boot card visible. `:remote-guide` displays the full workflow, and `:prepare-hid` switches a running human-mode session to high-contrast Machine Mode plus compact JSON output.

## Suggested remote sequence

1. Launch `scripts/Start-EasyChangeRemote.ps1 -Workspace '<workspace>'` or run `python -m easychange.gui <workspace> --machine --hid`.
2. Run `:locate NumeroProtocolo --context 2`; it returns a fresh result ID and nearby numbered lines together.
3. Edit the matched line with `:edit-result R1 "new text"`.
4. Run `:diff && :test` to inspect and validate in one submission.
5. Run `:undo` if the edit should be reverted.

The successful path takes three command submissions after launch (locate with context, edit, diff/test); undo adds one. EC1 commands carry a sequence, and Machine Mode renders compact text plus a QR payload in a fixed result area. B.M.O. reads it through HDMI. Search result IDs remain unique across commands and service restarts. Use `:batch ... :end` for atomic scripted sequences.

## Transport boundary

CLI, HTTP, MCP, and GUI adapters share `CommandService` when used locally. For REMOTE_PC, B.M.O. passes complete command strings through the GUI using ESP keyboard HID and reads the HDMI result area. The optional MCP adapter is never part of that remote path. Android relay guidance is in [ANDROID_REMOTE.md](ANDROID_REMOTE.md); the firmware-neutral timing/key contract is in [ESP32_HID_CONTRACT.md](ESP32_HID_CONTRACT.md). This repository does not flash ESP firmware or capture HDMI.

Do not expose the API to a network until authentication and an explicit permission model are implemented.
