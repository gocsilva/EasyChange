# EasyChange

EasyChange is a machine-first development workspace: a predictable command layer between an AI agent and a software workspace. The core works with any directory; project formats add optional build and test profiles.

## Install and run

Python 3.10 or newer is required. The core uses only the standard library.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,gui,api]"
python -m easychange C:\path\to\workspace
```

Run `easychange <workspace>` or `python -m easychange <workspace>` for the interactive CLI. Add `--json` for JSON output. Run the optional GUI with `python -m easychange.gui <workspace>`.

## Machine-first workflow

Commands accept a leading colon (optional in the CLI). Quoted arguments may contain spaces.

```text
:state
:files
:search NumeroProtocolo
:context R1
:replace-line src/example.py 12 "value = 2"
:diff && :test
:undo
```

The GUI uses **F12** for high-contrast Machine Mode, **Ctrl+K** to focus the command line, and **Esc** to return focus there. It includes line numbers, syntax highlighting, a persistent command field, and keyboard shortcuts. CLI and GUI use the same `CommandService`.

Use `:capabilities` to discover commands. Features include indexed search, line-numbered reads, file editing and metadata, symbol definitions/references, batches and command chains, aliases/macros, snapshots, locks, transactions, persistent journal/undo/redo, Git status/diff/branch/log, process/build/test, and diagnostics. High-level commands include `:rename-symbol`, `:create-class`, `:create-interface`, and `:create-test`. Format/import commands report `ADAPTER_UNAVAILABLE` until a language adapter implements them.

Output can be changed with `:set output text|json|compact`; `:set transport hid` selects compact output. File paths are confined to the opened workspace. External processes run as argument arrays, without shell expansion.

## Interfaces

- **CLI:** included, interactive and JSON-capable.
- **GUI:** optional PySide6 (`pip install -e ".[gui]"`).
- **HTTP API:** optional FastAPI/Uvicorn (`pip install -e ".[api]"`); loopback only by default: `python -m easychange.api.server <workspace>`. Routes provide state, capabilities, commands, search, bounded file read/write, build, and test.
- **MCP:** optional stdio server (`pip install -e ".[mcp]"`, then `python -m easychange.mcp.server <workspace>`). MCP tools call the shared command service.

## Adapters and persistence

Workspace detection recognizes Git, .NET, Python, Node, Rust, Go, Java, CMake, Unreal, and Godot markers without requiring them. Python, Node, and .NET adapters provide basic build/test profiles; unsupported build tools do not prevent generic browsing or editing.

Edits are recorded in a persistent `.easychange/` journal. Undo/redo checks file hashes; transactions can be recovered after restart. SQLite stores the incremental file index and cross-process locks. A generic regex provider supplies basic symbols and the provider interface can accept Tree-sitter or LSP integrations.

## B.M.O. and remote operation

Compact output with JSON payloads, persisted short IDs, line-numbered reads, batch commands, and the keyboard-focused GUI reduce HDMI/HID interaction cost. Start the remote UI with `scripts/Start-EasyChangeRemote.ps1 -Workspace '<project path>'`. MCP publishes an automatic remote guide/profile and can prepare or launch the HID GUI. Repository `AGENTS.md` and the Codex `easychange-remote-control` skill preserve these instructions for future agent tasks. See [REMOTE_CONTROL.md](docs/REMOTE_CONTROL.md), [ANDROID_REMOTE.md](docs/ANDROID_REMOTE.md), [PROTOCOL.md](docs/PROTOCOL.md), and [ARCHITECTURE.md](docs/ARCHITECTURE.md).

The current repository does not include Tree-sitter/LSP servers, authenticated network access, or physical HDMI/ESP32 firmware. The HTTP API binds to loopback by default.

## Tests

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```
