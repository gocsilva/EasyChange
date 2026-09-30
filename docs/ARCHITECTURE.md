# Architecture

EasyChange separates machine-facing intent from workspace mechanics so CLI, GUI, HTTP, and a future MCP transport share the same behavior.

```text
CLI / GUI / HTTP / future MCP
             |
       CommandService       (control plane)
             |
  Workspace, File, Search   (workspace plane)
             |
   Git, Process, Adapters   (execution plane)
             |
      Journal / TX          (state and safety)
```

## Core

`Workspace` is a root directory plus detected metadata. It does not require a solution or language marker. `Workspace.resolve` confines file operations to that root. `FileService` handles listing, bounded reads, writes, replacements, and line edits. `SearchService` provides a portable text search. `CommandService` is the shared command router and returns a structured `Result`.

## Adapters

The base `ProjectAdapter` protocol describes detection, capabilities, build, and test hooks. Generic browsing/editing always works. Detection currently recognizes Git and common .NET, Python, Node, Rust, Go, Java, CMake, Unreal, and Godot markers. Python, Node, and .NET have initial profile modules. Adapters do not own workspace semantics.

## Execution

`ProcessService` passes argv directly to subprocesses with a workspace working directory and timeout. `GitService` offers read-only status and diff operations. Build and test select detected adapter defaults or accept an explicit argv in the command.

## State and safety

The edit journal stores prior file contents in memory. Transactions buffer these changes until commit, or restore them in reverse order on rollback. Undo is available for committed journal entries. Path resolution blocks `..` and symlink escapes. Persistent journals, file locks, watcher-based external-change detection, and disk guards are not in this MVP.

## Presentation and transports

The CLI, optional PySide6 GUI, optional FastAPI application, and optional MCP stdio server call the same command service. HTTP binds to loopback by default. `remote.machine_protocol` provides a compact newline-friendly JSON envelope for HID/serial bridge integrations.

## Extension points

Add adapters under `easychange/adapters`, keeping format-specific rules outside the core. Add transports that translate input into `CommandService.execute` and serialize `Result`; they should not duplicate file-edit logic.
