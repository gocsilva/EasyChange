# EasyChange

EasyChange is a machine-first development workspace: a predictable command layer between an AI agent and a software workspace. Its core works with any directory; project formats add optional build and test profiles.

## Install and run

Python 3.10 or newer is required. The core uses only the standard library.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,gui,api]"
python -m easychange C:\path\to\workspace
```

Run `easychange <workspace>` or `python -m easychange <workspace>` to use the interactive CLI. Add `--json` for JSON command results. Run the optional GUI with `python -m easychange.gui <workspace>`.

## Machine-first workflow

Commands accept a leading colon (optional in the CLI). Quoted arguments may contain spaces.

```text
:state
:files
:search NumeroProtocolo
:read src/example.py 1 40
:replace-line src/example.py 12 "value = 2"
:diff
:undo
```

The GUI uses **F12** for Machine Mode, **Ctrl+K** to focus the command line, and **Esc** to return focus there. It has no modal dialogs in Machine Mode. CLI and GUI both call the same `CommandService`.

Supported core commands: `help`, `capabilities`, `state`, `workspace`, `pwd`, `files`, `tree`, `projects`, `open`, `read`, `head`, `tail`, `context`, `search`, `find`, `new`, `mkdir`, `write`, `append`, `insert`, `save`, `replace`, `replace-line`, `replace-range`, `delete`, `rename`, `begin`, `commit`, `rollback`, `undo`, `status`, `diff`, `build`, `test`, `run`, `set`, `machine`, `human`, `quit`.

Output can be changed with `:set output text|json|compact`; `:set transport hid` selects compact output. File paths are confined to the opened workspace. External processes are launched as argument arrays, without a shell.

## Interfaces

- **CLI:** included, interactive and JSON-capable.
- **GUI:** optional PySide6 (`pip install -e ".[gui]"`). Machine Mode is toggled with F12.
- **HTTP API:** optional FastAPI/Uvicorn (`pip install -e ".[api]"`); loopback only by default: `python -m easychange.api.server <workspace>`. It exposes `/api/state`, `/api/capabilities`, `/api/command`, and `/api/search`.
- **MCP:** optional stdio server (`pip install -e ".[mcp]"`, then `python -m easychange.mcp.server <workspace>`). Tools call the shared command service.

## Adapters

The generic adapter supports ordinary folders. Detection recognizes Git, .NET, Python, Node, Rust, Go, Java, CMake, Unreal, and Godot markers without requiring them. Python, Node, and .NET adapter modules currently provide basic build/test profiles; unsupported build tools do not prevent browsing or editing. Adapter capabilities are additive and core services remain language-neutral.

## State safety and Git

Edits enter an in-memory journal. `:undo` reverts the most recent file edit. `:begin`, `:commit`, and `:rollback` group edits; a rollback restores those edits in reverse order. Git status/diff are read-only. A workspace-specific `.easychange/` persistence format, external-change watcher, locks, LSP/Tree-sitter indexing, and persistent sessions are planned extensions.

## B.M.O. and remote operation

The compact output profile, stable short result IDs, line-numbered reads, and one-command-per-line protocol are intended to reduce HDMI/HID interaction cost. The remote protocol is local and transport-neutral; it does not itself control an ESP device. See [REMOTE_CONTROL.md](docs/REMOTE_CONTROL.md) and [PROTOCOL.md](docs/PROTOCOL.md).

## Tests

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

Architecture decisions and current scope: [ARCHITECTURE.md](docs/ARCHITECTURE.md). This repository is an MVP; consult that file for features not yet implemented.
