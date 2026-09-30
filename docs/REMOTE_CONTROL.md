# Remote control and B.M.O.

EasyChange is designed for concise command operation when an agent has a screen and keyboard. The GUI returns focus to its persistent command field after actions. **Ctrl+K** or **Esc** focuses the command field; **F12** toggles high-contrast Machine Mode.

The GUI opens with a `:remote` boot card visible. `:remote-guide` displays the full workflow, and `:prepare-hid` switches a running human-mode session to high-contrast Machine Mode plus compact JSON output.

## Suggested remote sequence

1. Launch `scripts/Start-EasyChangeRemote.ps1 -Workspace '<workspace>'` or run `python -m easychange.gui <workspace> --machine --hid`.
2. Run `:locate NumeroProtocolo --context 2`; it returns a fresh result ID and nearby numbered lines together.
3. Edit the matched line with `:edit-result R1 "new text"`.
4. Run `:diff && :test` to inspect and validate in one submission.
5. Run `:undo` if the edit should be reverted.

The successful path takes three command submissions after launch (locate with context, edit, diff/test); undo adds one. HID output is one compact line with a JSON payload, so IDs and error details remain available to OCR. Search result IDs remain unique across commands and service restarts. Commands can also be sent over CLI, loopback HTTP, or MCP without mouse interaction. Use `:batch ... :end` for scripted sequences.

## Transport boundary

CLI, HTTP, MCP, and GUI all call the same `CommandService`. MCP publishes `easychange://remote-guide` and a machine-readable profile, and provides host quickstart, batched workspace/search/edit/verify tools, and HID GUI launch tools. A B.M.O. bridge can pass complete command strings through the command field using ESP keyboard HID. Android relay guidance is in [ANDROID_REMOTE.md](ANDROID_REMOTE.md); the firmware-neutral timing/key contract is in [ESP32_HID_CONTRACT.md](ESP32_HID_CONTRACT.md). This repository does not flash ESP firmware, capture HDMI, or expose a network listener beyond the loopback-only API.

Do not expose the API to a network until authentication and an explicit permission model are implemented.
