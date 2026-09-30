from __future__ import annotations

from pathlib import Path


PROFILE_VERSION = "1.0"

REMOTE_PROFILE = {
    "profile_version": PROFILE_VERSION,
    "product": "EasyChange",
    "purpose": "Operate the selected software workspace through MCP, CLI, or HDMI plus ESP32 keyboard HID.",
    "principles": [
        "Use MCP tools directly when connected; do not route MCP-capable work through OCR or simulated keystrokes.",
        "When only HDMI and ESP32 HID are available, use the EasyChange GUI command field and short commands.",
        "The active workspace is the root bound to the current EasyChange service; keep paths inside it.",
        "Inspect structured command results and hashes before or after consequential edits; edits are journaled and undoable.",
        "Do not assume Android has MCP or HID access: use its configured remote desktop/keyboard bridge, or its MCP client if one is explicitly connected.",
    ],
    "hid_startup": {
        "gui_command": "python -m easychange.gui <workspace> --machine --hid",
        "first_commands": [":remote", ":state", ":capabilities"],
        "command_focus": ["Ctrl+K", "Escape"],
        "machine_mode_toggle": "F12",
        "submit": "Enter",
        "result_format": "One-line compact result with a JSON data payload; preserve returned F/R/C IDs.",
    },
    "workflow": [
        ":state",
        ":search <term>",
        ":context <R-id>",
        ":replace-line <path> <line> <text>",
        ":diff && :test",
        ":undo  (only when requested or validation indicates the change should be reverted)",
    ],
    "mcp_tools": ["state", "capabilities", "command", "remote_quickstart", "prepare_hid_session", "launch_hid_gui",
                  "search", "read_file", "edit_file", "git_diff", "test", "undo"],
    "android_modes": {
        "remote_desktop": "Open the remote EasyChange GUI session, keep the command input focused, and use the HID workflow.",
        "mcp_client": "Connect to the EasyChange MCP server running on the same host as the selected workspace and use its remote guide first.",
        "phone_only_chat": "A phone chat alone cannot control the remote PC; an authorized desktop or MCP bridge must be connected.",
    },
    "known_limits": [
        "The stdio MCP server is not a network server and cannot be reached directly across devices.",
        "The loopback HTTP API is local-only by default and has no authentication for network exposure.",
        "EasyChange does not install ESP firmware, capture HDMI, or pair Android remote-control applications.",
    ],
}


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def remote_quickstart(workspace: Path, python: str) -> dict:
    root = str(workspace.resolve())
    executable = str(Path(python).resolve())
    return {
        "profile_version": PROFILE_VERSION,
        "workspace": root,
        "gui_command": f"& {powershell_quote(executable)} -m easychange.gui {powershell_quote(root)} --machine --hid",
        "gui_setup_command": f"& {powershell_quote(executable)} -m pip install PySide6",
        "mcp_server_command": f"& {powershell_quote(executable)} -m easychange.mcp.server {powershell_quote(root)}",
        "mcp_config": {"transport": "stdio", "command": executable,
                       "args": ["-m", "easychange.mcp.server", root]},
        "first_commands": PROFILE_FIRST_COMMANDS,
        "shortcuts": REMOTE_PROFILE["hid_startup"],
        "android_modes": REMOTE_PROFILE["android_modes"],
        "limits": REMOTE_PROFILE["known_limits"],
    }


PROFILE_FIRST_COMMANDS = [":remote", ":state", ":capabilities"]


def remote_guide_markdown() -> str:
    return """# EasyChange remote operator guide

This guide is served by MCP so connected agents can discover the correct workflow without a user re-explaining it.

## Choose the available path

1. **MCP connected:** call `state`, then `capabilities`; use structured EasyChange tools directly. Do not use OCR/HID when MCP can perform the operation.
2. **Only HDMI + ESP32 keyboard HID:** launch `python -m easychange.gui <workspace> --machine --hid`; focus the command field with Ctrl+K; submit short EasyChange commands with Enter. Compact output includes JSON data so result IDs and errors remain visible to OCR.
3. **Android:** use an already-configured remote desktop/keyboard bridge to reach the remote GUI, or connect an MCP client to a server on the same host as the workspace. A phone chat by itself cannot control the PC.

## Safe, low-interaction workflow

The GUI displays a `:remote` boot card; run `:remote-guide` for this guide or `:prepare-hid` to switch a human-mode GUI into high-contrast compact mode. Then run `:state`, `:search <term>`, `:context <R-id>`, edit with `:replace-line` or `:write`, and finish with `:diff && :test`. Keep IDs and paths from the returned data. Use `:undo` only when requested or when validation calls for reverting. All paths must remain within the workspace. For conflicts, inspect `:diff` or use `:reload`; use `:force-write` only when overwriting the external change is intended.

## Useful GUI controls

- Ctrl+K or Escape: focus command field
- F12: toggle Machine Mode
- Enter: run command
- Ctrl+S: save current editor buffer
- Ctrl+D: Git diff
- F5 / F6: build / test

The MCP server is stdio-only; it is not reachable over the network. The HTTP API is loopback-only by default. EasyChange does not install or control ESP firmware, HDMI capture, or Android remote-control apps.
"""
