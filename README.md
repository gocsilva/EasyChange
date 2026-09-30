# EasyChange

EasyChange is a machine-first development workspace: a predictable command layer between an AI agent and a software workspace. The core works with any directory; project formats add optional build and test profiles.

## Install and run

Python 3.10 or newer is required. The core uses only the standard library.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,gui]"
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

Use `:capabilities` to discover commands. Features include indexed search, line-numbered reads, file editing and metadata, symbol definitions/references, batches and command chains, aliases/macros, snapshots, locks, transactions, persistent journal/undo/redo, Git status/diff/branch/log, process/build/test, and diagnostics. Runtime-aware commands add `:runtime-profile`, `:run-project`, `:test-smart`, `:swagger-evidence`, `:evidence`, `:process-logs`, `:db-connections`, `:db-schema`, and `:db-query`. They are optional capabilities: normal browsing/editing never depends on them. High-level commands include `:rename-symbol`, `:create-class`, `:create-interface`, and `:create-test`. Format/import commands report `ADAPTER_UNAVAILABLE` until a language adapter implements them.

Output can be changed with `:set output text|json|compact`; `:set transport hid` selects compact output. File paths are confined to the opened workspace. External processes run as argument arrays, without shell expansion.

## Interfaces

- **Machine GUI:** the production AI control surface. Commands enter only through keyboard/HID and results leave through the visible HDMI surface using compact EC1 text plus chunked EC2 optical QR packets.
- **CLI:** retained for local diagnostics, development, and automated tests.
- **No HTTP or MCP server is embedded in EasyChange.** B.M.O./MCP remains outside EasyChange and may interact with it only through the physical ESP32 HID + HDMI boundary.

## Adapters and persistence

Workspace detection recognizes Git, .NET, Python, Node, Rust, Go, Java, CMake, Unreal, and Godot markers without requiring them. Python, Node, and .NET adapters provide basic build/test profiles; unsupported build tools do not prevent generic browsing or editing.

Edits are recorded in a persistent `.easychange/` journal. Undo/redo checks file hashes; transactions can be recovered after restart. SQLite stores the incremental file index and cross-process locks. A generic regex provider supplies basic symbols and the provider interface can accept Tree-sitter or LSP integrations.

## B.M.O. and remote operation

Compact results, persistent IDs, ranked `:locate`, `:study`, `:read-many`, atomic batches, EC1 request sequences, EC2 chunked/compressed QR results, CRC/SHA verification, and the keyboard-focused Machine GUI reduce HDMI/HID round trips. Start the remote UI with `scripts/Start-EasyChangeRemote.ps1 -Workspace '<project path>'`. See [REMOTE_BOUNDARY.md](docs/REMOTE_BOUNDARY.md), [REMOTE_CONTROL.md](docs/REMOTE_CONTROL.md), [ANDROID_REMOTE.md](docs/ANDROID_REMOTE.md), [ESP32_HID_CONTRACT.md](docs/ESP32_HID_CONTRACT.md), [PROTOCOL.md](docs/PROTOCOL.md), and [ARCHITECTURE.md](docs/ARCHITECTURE.md).

The current repository does not include Tree-sitter/LSP servers, network control endpoints, or physical HDMI/ESP32 firmware. EasyChange itself stays transport-agnostic and visible; the external controller owns ESP32 HID and HDMI capture.

## Runtime, Swagger evidence, and databases

EasyChange can detect runnable/testable .NET API, worker, console, function and test projects, plus common Node, Python, Go, Rust, Maven and Gradle workspaces.

```text
:runtime-profile
:run-project
:test-smart
:swagger-evidence --profile dotnet:src/My.Api/My.Api.csproj
:evidence --title endpoint-smoke
```

Swagger evidence is saved under `.easychange/evidence/` and can include the HTTP response, process logs, a Markdown report, and a headless Edge/Chrome screenshot when a supported browser is installed.

SQL is **read-only by default**. SQLite can be queried directly by workspace-relative file name. SQL Server, PostgreSQL and MySQL use their local command-line clients when installed. Named connections live in `.easychange/db_connections.json`; passwords/tokens should be referenced through environment-variable names rather than stored in that file.

```json
{
  "connections": {
    "local-sqlserver": {
      "provider": "sqlserver",
      "server_env": "APP_DB_SERVER",
      "database_env": "APP_DB_NAME",
      "user_env": "APP_DB_USER",
      "password_env": "APP_DB_PASSWORD"
    }
  }
}
```

```text
:db-connections
:db-schema local-sqlserver
:db-query local-sqlserver "SELECT TOP 20 * FROM dbo.Example"
```

Mutating SQL requires an explicit `--write` flag. Database access, runtime execution, Swagger evidence, and all other advanced capabilities remain optional; EasyChange stays usable as a generic deterministic editor without them.

## Tests

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```
