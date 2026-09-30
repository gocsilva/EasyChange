# ESP32 HID operator contract

This is the EasyChange-side operator contract for an ESP32 acting as a keyboard HID device with EasyChange visible over HDMI. In the B.M.O. installation, the existing Smart HID v3 protocol provides block text transfer and bounded composite jobs. Check the connected device's `CAPABILITIES` before using optional primitives; EasyChange itself remains independent of board model, USB/BLE transport, and firmware release.

## Expected interaction

1. The PC has EasyChange open in Machine/HID mode. Its command input receives focus after every command.
2. The controller transfers one complete command line through the bridge's block text operation, then sends **Enter once**. Avoid a serial/HID round trip per character.
3. The agent waits until the output panel finishes updating, then captures the HDMI frame and reads the one-line result, including its JSON payload.
4. It copies result IDs and paths exactly into the next command. It does not repeat Enter or replay a command because OCR is slow; first inspect the latest command ID/result.

The usual successful exchange is three command submissions: `:locate <term> --context 2`, `:edit-result <R-id> <replacement line>`, and `:diff && :test`; `:undo` is a separate recovery action when requested. `:locate` combines search and source context, reducing one request/capture. On REMOTE_PC, all command input uses ESP32 HID and all results use HDMI; optional MCP tools are only for MCP_HOST-local workspaces. Machine Mode includes compact text and a QR sequence packet. B.M.O. Smart HID v3 can batch short related keyboard actions in one bounded job; wait for its terminal event and verify the semantic result over HDMI.

## Character and key rules

- Send one command line per Enter. Preserve quotes around arguments with spaces; the command parser uses shell-like quotes.
- Use Ctrl+K only to restore focus, not on every action. The GUI returns focus to the command field itself.
- Do not assume Ctrl+V, IME-specific input, or key-repeat works. Use the installed block text-transfer path and send Enter once.
- Do not send a second command until the last output is visible. Use its `C` command ID to distinguish the new response from a stale HDMI frame.
- In HID mode, compact output is a single line: `OK C… command …ms {JSON}` or `ERR C… CODE command {JSON}`. It includes result data, so search IDs and errors are not hidden.

## Test and limits

The simulated HID benchmark measures command submissions and approximate keystrokes, but cannot validate physical typing latency, keyboard layout, BLE/USB reliability, or HDMI capture delay. This repository does not flash firmware. Before connecting a new ESP32 firmware build, verify its advertised profile and keyboard layout on a harmless command such as `:state`; an ESP ACK confirms HID generation, not that EasyChange accepted or completed the command.
