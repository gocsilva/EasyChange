from __future__ import annotations

from typing import Any

from .operation_registry import TEXT_MUTATION_COMMANDS


COMMAND_NAMES = [
    "state", "workspace", "pwd", "files", "tree", "next", "prev",
    "read", "head", "tail", "context", "goto", "search", "find", "index",
    "symbols", "symbol", "definition", "references", "implementations",
    "outline", "file-summary", "locate", "study", "discover-many",
    "read-many", "validate", "edit-result", "runtime-profile", "run-project",
    "test-smart", "test-evidence", "swagger-evidence", "evidence",
    "process-logs", "db-connections", "db-schema", "db-query", "new",
    "mkdir", "write", "append", "insert", "replace", "replace-line",
    "replace-range", "delete", "rename", "move", "stat", "hash", "exists",
    "rename-symbol", "create-class", "create-interface", "create-test",
    "format", "fix-imports", "organize-imports", "journal", "undo", "redo",
    "begin", "commit", "rollback", "snapshot", "lock", "unlock", "locks",
    "watch", "batch", "macro", "alias", "complete", "history", "repeat",
    "clear", "status", "health", "maintenance", "diff", "branch", "log",
    "build", "test", "run", "processes", "stop", "errors", "next-error",
    "previous-error", "remote", "remote-guide", "remote-profile",
    "prepare-hid", "set", "machine", "human", "help", "capabilities",
    "command-schema", "quit",
]


_RICH: dict[str, dict[str, Any]] = {
    "replace": {
        "description": "Replace exact text in one workspace file; fails closed when the old text is ambiguous or absent.",
        "syntax": "replace <path> <old_text> <new_text>",
        "arguments": [
            {"name": "path", "required": True, "type": "workspace_path"},
            {"name": "old_text", "required": True, "type": "text"},
            {"name": "new_text", "required": True, "type": "text"},
        ],
        "examples": [
            'replace src/Foo.cs "oldValue" "newValue"',
            'replace tests/FooTests.cs "Returns(old)" "Returns(Task.FromException<ValueResult<X>>(ex))"',
        ],
    },
    "read-many": {
        "description": "Read up to 64 files with byte-budgeted deterministic continuation.",
        "syntax": "read-many <path...> [--count N] [--max-bytes N] [--cursor CURSOR]",
        "arguments": [
            {"name": "path", "required": True, "repeatable": True, "max_items": 64},
            {"name": "--count", "required": False, "type": "int", "default": 240},
            {"name": "--max-bytes", "required": False, "type": "int", "default": 2097152},
            {"name": "--cursor", "required": False, "type": "opaque_cursor"},
        ],
        "examples": [
            "read-many src/A.cs src/B.cs --count 200 --max-bytes 262144",
            "read-many src/A.cs src/B.cs --count 200 --max-bytes 262144 --cursor v1:1:73:128",
        ],
    },
    "batch": {
        "description": "Execute a bounded command batch. Mutating structured batches use the atomic planner when eligible.",
        "syntax": "batch <command1> ; <command2> ; ...",
        "arguments": [{"name": "commands", "required": True, "repeatable": True}],
        "examples": ['batch "read src/A.cs" ; "read src/B.cs"'],
    },
    "help": {
        "description": "List commands or return structured metadata for one command.",
        "syntax": "help [command]",
        "arguments": [{"name": "command", "required": False, "type": "command_name"}],
        "examples": ["help", "help replace"],
    },
    "command-schema": {
        "description": "Return machine-readable metadata for one EasyChange text command.",
        "syntax": "command-schema <command>",
        "arguments": [{"name": "command", "required": True, "type": "command_name"}],
        "examples": ["command-schema replace"],
    },
    "diff": {
        "description": "Read Git/workspace diff information without mutating the project.",
        "syntax": "diff [path]",
        "arguments": [{"name": "path", "required": False, "type": "workspace_path"}],
        "examples": ["diff", "diff src/Foo.cs"],
    },
    "test-smart": {
        "description": "Run the detected project test strategy and return a compact structured result.",
        "syntax": "test-smart [profile] [timeout_seconds]",
        "arguments": [
            {"name": "profile", "required": False, "type": "runtime_profile"},
            {"name": "timeout_seconds", "required": False, "type": "int"},
        ],
        "examples": ["test-smart", "test-smart worker-tests 600"],
    },
    "runtime-profile": {
        "description": "Inspect or configure an EasyChange runtime profile.",
        "syntax": "runtime-profile [profile]",
        "arguments": [{"name": "profile", "required": False, "type": "runtime_profile"}],
        "examples": ["runtime-profile"],
    },
    "maintenance": {
        "description": "Run bounded EasyChange-owned cleanup; never removes project source files.",
        "syntax": "maintenance [--dry-run] [--workspace-temps]",
        "arguments": [
            {"name": "--dry-run", "required": False, "type": "flag"},
            {"name": "--workspace-temps", "required": False, "type": "flag"},
        ],
        "examples": ["maintenance --dry-run", "maintenance"],
    },
}


_READ_ONLY = {
    "state", "workspace", "pwd", "files", "tree", "next", "prev", "read",
    "head", "tail", "context", "goto", "search", "find", "symbols", "symbol",
    "definition", "references", "implementations", "outline", "file-summary",
    "locate", "study", "discover-many", "read-many", "stat", "hash", "exists",
    "journal", "locks", "history", "status", "health", "diff", "branch", "log",
    "processes", "errors", "next-error", "previous-error", "remote-guide",
    "help", "capabilities", "command-schema", "db-connections", "db-schema",
}


def command_schema(name: str) -> dict[str, Any]:
    normalized = str(name or "").strip().lstrip(":").casefold()
    if normalized not in COMMAND_NAMES:
        raise LookupError(f"UNKNOWN_COMMAND:{normalized or '<empty>'}")
    mutating = normalized in TEXT_MUTATION_COMMANDS or normalized not in _READ_ONLY
    data = dict(_RICH.get(normalized) or {})
    data.setdefault(
        "description",
        f"EasyChange command '{normalized}'. Use the machine-readable metadata to discover its execution properties.",
    )
    data.setdefault("syntax", normalized + (" [args...]" if normalized not in {"state", "pwd", "status", "health", "capabilities", "quit"} else ""))
    data.setdefault("arguments", [])
    data.setdefault("examples", [data["syntax"]])
    data.update({
        "name": normalized,
        "mutating": bool(mutating),
        "read_only": not bool(mutating),
        "max_payload_bytes": 50 * 1024,
        "supports_sequence": True,
        "supports_transaction": bool(normalized in TEXT_MUTATION_COMMANDS or normalized in {"begin", "commit", "rollback", "batch"}),
        "supports_rollback": bool(normalized in TEXT_MUTATION_COMMANDS or normalized in {"rollback", "batch"}),
    })
    return data


def command_catalog() -> list[dict[str, Any]]:
    return [command_schema(name) for name in COMMAND_NAMES]
