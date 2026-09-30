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

The GUI uses **F12** for high-contrast Machine Mode, **Ctrl+K** to focus the command line, and **Esc** to return focus there. It includes line numbers, syntax highlighting, a persistent command field, and keyboard shortcuts. Use **Selecionar projeto…** in the header to open another folder in the same window. On Windows, double-click `Abrir EasyChange.bat` to start the GUI; it initially opens the EasyChange repository.

Use `:capabilities` to discover commands. Features include indexed search, line-numbered reads, file editing and metadata, symbol definitions/references, batches and command chains, aliases/macros, snapshots, locks, transactions, persistent journal/undo/redo, Git status/diff/branch/log, process/build/test, and diagnostics. High-level commands include `:rename-symbol`, `:create-class`, `:create-interface`, and `:create-test`. Format/import commands report `ADAPTER_UNAVAILABLE` until a language adapter implements them.

Output can be changed with `:set output text|json|compact`; `:set transport hid` selects compact output. File paths are confined to the opened workspace. External processes run as argument arrays, without shell expansion.

## Interfaces

- **CLI:** included, interactive and JSON-capable.
- **GUI:** optional PySide6 (`pip install -e ".[gui]"`).
- **HTTP API:** optional FastAPI/Uvicorn for MCP_HOST-local workspaces (`pip install -e ".[api]"`); loopback only by default. This API is not a REMOTE_PC control path.
- **MCP:** optional stdio server for MCP_HOST-local workspaces (`pip install -e ".[mcp]"`). Do not install or launch it on REMOTE_PC for B.M.O. control; remote input is ESP32 HID and output is HDMI.

## Adapters and persistence

Workspace detection recognizes Git, .NET, Python, Node, Rust, Go, Java, CMake, Unreal, and Godot markers without requiring them. Python, Node, and .NET adapters provide basic build/test profiles; unsupported build tools do not prevent generic browsing or editing.

Edits are recorded in a persistent `.easychange/` journal. Undo/redo checks file hashes; transactions can be recovered after restart. SQLite stores the incremental file index and cross-process locks. A generic regex provider supplies basic symbols and the provider interface can accept Tree-sitter or LSP integrations.

## B.M.O. and remote operation

Compact results, persistent IDs, `:locate`, `:edit-result`, atomic batches, EC1 sequences, CRC-checked compression, and the keyboard-focused GUI reduce HDMI/HID interactions. Start the remote UI with `scripts/Start-EasyChangeRemote.ps1 -Workspace '<project path>'`. MCP-local tools are for MCP_HOST workspaces only. See [REMOTE_BOUNDARY.md](docs/REMOTE_BOUNDARY.md), [REMOTE_CONTROL.md](docs/REMOTE_CONTROL.md), [ANDROID_REMOTE.md](docs/ANDROID_REMOTE.md), [ESP32_HID_CONTRACT.md](docs/ESP32_HID_CONTRACT.md), [PROTOCOL.md](docs/PROTOCOL.md), and [ARCHITECTURE.md](docs/ARCHITECTURE.md).

The current repository does not include Tree-sitter/LSP servers, authenticated network access, or physical HDMI/ESP32 firmware. The HTTP API binds to loopback by default.

## Tests

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```
