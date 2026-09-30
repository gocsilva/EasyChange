# Remote control and B.M.O.

EasyChange is designed for concise command operation when an agent has a screen and keyboard. The GUI returns focus to its persistent command field after actions. **Ctrl+K** or **Esc** focuses the command field; **F12** toggles high-contrast Machine Mode.

## Suggested remote sequence

1. Launch `python -m easychange <workspace>` or the GUI.
2. Run `:search NumeroProtocolo` and keep the `R1` result ID.
3. Run `:context R1` to read nearby lines.
4. Edit with `:replace-line <path> <line> "new text"`.
5. Run `:diff && :test` to inspect and validate in one submission.
6. Run `:undo` if the edit should be reverted.

The successful path takes four command submissions after launch (search, context, edit, diff/test); undo adds one. Commands can be sent over CLI, loopback HTTP, or MCP without mouse interaction. Use `:set transport hid` for compact responses and `:batch ... :end` for scripted sequences.

## Transport boundary

CLI, HTTP, MCP, and GUI all call the same `CommandService`. `easychange.remote.machine_protocol` encodes a structured JSON result and validates a JSON command envelope. A B.M.O. bridge can pass complete command strings through this boundary. This repository does not include ESP firmware, HDMI capture, HID hardware control, or a network listener beyond the loopback-only API.

Do not expose the API to a network until authentication and an explicit permission model are implemented.
