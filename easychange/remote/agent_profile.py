from __future__ import annotations

from pathlib import Path


PROFILE_VERSION = "1.1"

REMOTE_PROFILE = {
    "profile_version": PROFILE_VERSION,
    "product": "EasyChange",
    "purpose": "Operate EasyChange on REMOTE_PC through ESP32 HID input and HDMI output; MCP/API transports are MCP_HOST-local only.",
    "principles": [
        "On REMOTE_PC, use only the EasyChange GUI through physical ESP32 HID input and HDMI observation.",
        "Never run or connect an MCP server/client, HTTP/API, shared filesystem, clipboard, SSH, or other B.M.O. channel on REMOTE_PC.",
        "Optional MCP and HTTP adapters are for workspaces local to MCP_HOST; never use them to control REMOTE_PC.",
        "Use one TYPE_TEXT block ending in newline for a short command; use SmartHidHost block transfer for long content.",
        "The active workspace is the root bound to the current EasyChange service; keep paths inside it.",
        "Inspect structured command results and hashes before or after consequential edits; edits are journaled and undoable.",
        "Android video relay is output only; do not infer keyboard input or an MCP route from a phone chat.",
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
        ":locate <term> --context 2",
        ":edit-result <R-id> <replacement line>",
        ":diff && :test",
        ":undo  (only when requested or validation indicates the change should be reverted)",
    ],
    "local_mcp_tools": ["state", "capabilities", "command", "remote_quickstart", "prepare_hid_session", "launch_hid_gui",
                  "workspace_brief", "search_context", "apply_edit_plan", "verify_change",
                  "search", "read_file", "edit_file", "git_diff", "test", "undo"],
    "android_modes": {
        "remote_desktop": "Open the remote EasyChange GUI session, keep the command input focused, and use the HID workflow.",
        "mcp_client": "MCP is not an allowed REMOTE_PC control path; MCP adapters may be used only for MCP_HOST-local workspaces.",
        "phone_only_chat": "A phone chat alone cannot control the remote PC; the authorized physical input is ESP32 HID.",
    },
    "known_limits": [
        "The stdio MCP server is not a network server and cannot be reached directly across devices.",
        "The loopback HTTP API is local-only by default and has no authentication for network exposure.",
        "Do not install or launch the optional MCP/API adapter on REMOTE_PC for B.M.O. control.",
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
        "first_commands": PROFILE_FIRST_COMMANDS,
        "shortcuts": REMOTE_PROFILE["hid_startup"],
        "android_modes": REMOTE_PROFILE["android_modes"],
        "limits": REMOTE_PROFILE["known_limits"],
    }


PROFILE_FIRST_COMMANDS = [":remote", ":state", ":capabilities"]


def remote_guide_markdown() -> str:
    return """# EasyChange remote operator guide

This guide is served as product help. For REMOTE_PC, EasyChange is a local GUI controlled only by physical ESP32 HID input and HDMI output. Optional MCP/API adapters are MCP_HOST-local tools and must not run on REMOTE_PC.

## Choose the available path

1. **REMOTE_PC:** launch `python -m easychange.gui <workspace> --machine --hid`; B.M.O. sends commands through ESP32 HID and reads only HDMI. Do not start/connect EasyChange MCP/API on that computer.
2. **MCP_HOST-local workspace:** optional local MCP or HTTP adapters may be used when the workspace itself is on MCP_HOST. They are not a remote control path.
3. **Android:** video relay is output-only unless a separate authorized ESP32 HID connection exists. A phone chat alone cannot control the PC.

## Safe, low-interaction workflow

The GUI displays a `:remote` boot card; run `:remote-guide` for this guide or `:prepare-hid` to switch a human-mode GUI into high-contrast compact mode. Then use `:locate <term> --context 2` to get result IDs and surrounding lines in one call, and `:edit-result <R-id> <replacement line>` for a single-line change. Finish with `:diff && :test`. Keep IDs and paths from the returned data. Use `:undo` only when requested or when validation calls for reverting. All paths must remain within the workspace. For conflicts, inspect `:diff` or use `:reload`; use `:force-write` only when overwriting the external change is intended.

## Useful GUI controls

- Ctrl+K or Escape: focus command field
- F12: toggle Machine Mode
- Enter: run command
- Ctrl+S: save current editor buffer
- Ctrl+D: Git diff
- F5 / F6: build / test

The MCP server is stdio-only and HTTP is loopback-only by default; neither is a B.M.O.↔REMOTE_PC channel. EasyChange does not install or control ESP firmware, HDMI capture, or Android remote-control apps.
"""
