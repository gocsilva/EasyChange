# Remote control and B.M.O.

EasyChange is designed for concise command operation when an agent has a screen and keyboard. The GUI returns focus to its persistent command field after actions. **Ctrl+K** or **Esc** focuses the command field; **F12** toggles high-contrast Machine Mode.

The GUI opens with a `:remote` boot card visible. `:remote-guide` displays the full workflow, and `:prepare-hid` switches a running human-mode session to high-contrast Machine Mode plus compact JSON output.

## Suggested remote sequence

1. Launch `scripts/Start-EasyChangeRemote.ps1 -Workspace '<workspace>'` or run `python -m easychange.gui <workspace> --machine --hid`.
2. Run `:search NumeroProtocolo` and keep the `R1` result ID.
3. Run `:context R1` to read nearby lines.
4. Edit with `:replace-line <path> <line> "new text"`.
5. Run `:diff && :test` to inspect and validate in one submission.
6. Run `:undo` if the edit should be reverted.

The successful path takes four command submissions after launch (search, context, edit, diff/test); undo adds one. In HID mode each result is a compact single line with a JSON data payload, so IDs and error details remain available to OCR. Commands can also be sent over CLI, loopback HTTP, or MCP without mouse interaction. Use `:batch ... :end` for scripted sequences.

## Transport boundary

CLI, HTTP, MCP, and GUI all call the same `CommandService`. MCP publishes `easychange://remote-guide` and a machine-readable profile, and provides `remote_quickstart`, `prepare_hid_session`, and `launch_hid_gui` tools. `easychange.remote.machine_protocol` encodes a structured JSON result and validates a JSON command envelope. A B.M.O. bridge can pass complete command strings through the command field using ESP keyboard HID. Android relay guidance is in [ANDROID_REMOTE.md](ANDROID_REMOTE.md). This repository does not include ESP firmware, HDMI capture, or a network listener beyond the loopback-only API.

Do not expose the API to a network until authentication and an explicit permission model are implemented.
