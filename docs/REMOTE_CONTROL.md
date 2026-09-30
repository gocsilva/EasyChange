# Remote control and B.M.O.

EasyChange is designed to be operated through concise text commands when an agent only has a screen and keyboard. The command line is always the focus target after GUI commands. **Ctrl+K** focuses it; **F12** toggles a high-contrast Machine Mode; **Esc** returns to the command input.

## Suggested remote sequence

1. Launch `python -m easychange <workspace>` (or the GUI) once.
2. Run `:search NumeroProtocolo` and retain the `R1` result ID / path.
3. Run `:read <path> <line> 20` for exact line-numbered context.
4. Edit with `:replace-line <path> <line> "new text"`.
5. Inspect `:diff`, run `:test`, and use `:undo` if needed.

This is seven command submissions after application launch, including search, read, edit, diff, test, and undo if the test outcome calls for a revert (six when no revert is needed). Use `:set transport hid` to shorten responses.

## Transport boundary

CLI, HTTP, and future MCP all call `CommandService`; the GUI does the same. `easychange.remote.machine_protocol` encodes one JSON result and validates a JSON command envelope. A B.M.O. bridge can pass complete command strings through this boundary. There is no device-specific ESP firmware, HDMI capture, authentication handshake, or network listener in this MVP.

The HTTP API listens only on `127.0.0.1` by default. Do not expose it to a network until authentication and an explicit permission model are implemented.
