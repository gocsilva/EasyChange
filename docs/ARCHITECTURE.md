# Architecture

EasyChange separates machine-facing intent from workspace mechanics. CLI, GUI, HTTP, and MCP share one `CommandService` and structured `Result` model.

```text
CLI / GUI / HTTP / MCP
          |
    CommandService ---- MachineSession / IDs / command history
          |
 Workspace / FileService / SQLite Indexer / SymbolService / Watcher
          |
 GitService / ProcessService / language adapters
          |
 Journal / transactions / cross-process locks
```

## Generic workspace

`Workspace.open` accepts any directory. Workspace detection adds metadata and optional adapters; browsing, indexing, search, edits, and Git do not require a `.sln` or another project marker. `Workspace.resolve` confines paths to the workspace, including symlink resolution.

`FileService` handles bounded text reads and writes, metadata, hashes, line edits, and external-change checks. Binary files are not treated as text. The SQLite index records file path, extension, size, mtime, hash, language, and bounded UTF-8 content. It refreshes changed files and removes deleted entries. `SymbolService` accepts pluggable providers; a generic regex provider supplies basic class/function symbols while Tree-sitter and LSP providers remain extension points.

## Adapters and execution

Project adapters add detection and build/test profiles without owning generic workspace behavior. Python, Node, and .NET profiles are currently supplied. `ProcessService` launches argument arrays with `shell=False`, captures bounded output, and supports background processes. Git status, branch, log, and diff are exposed separately.

## State and safety

Machine session state, short-ID counters, aliases, macros, file references, and recent results persist under `.easychange/`. A JSONL journal stores reversible file changes; transactions survive process restart until committed or rolled back. Undo and redo check current hashes before changing a file. File locks are stored in SQLite and shared across service instances with expiry. Writes detect changes since the file was read or indexed; `:reload`, `:diff`, and `:force-write` let the caller handle conflicts.

The watcher polls the workspace and refreshes index deltas. Large and binary text reads are bounded. External commands run without shell interpolation. The HTTP server rejects non-loopback bind addresses by default.

## GUI and transports

The optional PySide6 GUI is keyboard-driven, has a persistent command input, line-numbered editor, syntax highlighting, high-contrast Machine Mode, and shortcuts for search, open, save, diff, build, test, diagnostics, undo, and redo. CLI, HTTP, and MCP are adapters around the same command service. The API offers state, capabilities, command, search, bounded file read/write, build, and test routes. MCP runs over stdio.

The compact protocol and `:set transport hid` reduce output volume for a future HDMI/HID bridge. This repository does not include ESP firmware, HDMI capture, HID hardware control, authentication, or a network-facing remote agent service.
