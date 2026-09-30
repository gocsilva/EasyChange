from __future__ import annotations

import shlex
import time
from dataclasses import dataclass

from .file_service import FileService
from .git_service import GitService
from .ids import IdFactory
from .process_service import ProcessService
from .result import Result
from .search_service import SearchService
from .workspace import Workspace


@dataclass
class Change:
    path: str
    before: str | None
    after: str


class CommandService:
    """Single command plane shared by CLI, GUI, HTTP, and future MCP transports."""
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.files = FileService(workspace)
        self.searcher = SearchService(workspace)
        self.git = GitService(workspace)
        self.processes = ProcessService(workspace.root_path)
        self.ids = IdFactory()
        self.output = "text"
        self.machine = False
        self.history: list[str] = []
        self.journal: list[Change] = []
        self.transaction: list[Change] | None = None
        self.results: dict[str, dict] = {}
        self.file_refs: dict[str, str] = {}

    def execute(self, command: str) -> Result:
        raw = command.strip()
        try:
            tokens = _parse_tokens(raw[1:] if raw.startswith(":") else raw)
            if not tokens:
                raise ValueError("Empty command")
            return self.execute_tokens(tokens[0], tokens[1:], raw=raw)
        except Exception as exc:
            return Result(False, raw.split(maxsplit=1)[0].lstrip(":").lower() if raw else "", error=str(exc),
                          code=_error_code(exc), command_id=self.ids.next("C"))

    def execute_tokens(self, name: str, args: list[str], *, raw: str | None = None) -> Result:
        """Execute already-tokenized input for transports such as MCP."""
        started = time.monotonic()
        name = name.lower()
        self.history.append(raw if raw is not None else ":" + " ".join([name, *args]))
        try:
            data = self._dispatch(name, list(args))
            result = Result(True, name, data=data, command_id=self.ids.next("C"))
        except Exception as exc:
            result = Result(False, name, error=str(exc), code=_error_code(exc), command_id=self.ids.next("C"))
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    def _dispatch(self, name: str, args: list[str]) -> dict:
        if name in {"help", "capabilities"}:
            return {"commands": ["state", "workspace", "pwd", "files", "tree", "read", "head", "tail", "context",
                    "search", "find", "new", "mkdir", "write", "append", "insert", "replace", "replace-line", "replace-range", "delete", "rename", "save", "undo",
                    "begin", "commit", "rollback", "status", "diff", "build", "test", "run", "set", "machine", "human", "quit"]}
        if name == "state":
            return {"state": "READY", "workspace": str(self.workspace.root_path), "workspace_id": "W1",
                    "name": self.workspace.name, "type": self.workspace.kind, "adapters": self.workspace.adapters,
                    "git": self.git.status() if "GIT" in self.workspace.adapters else None,
                    "transaction": "ACTIVE" if self.transaction is not None else "NONE", "machine_mode": self.machine}
        if name in {"pwd", "workspace"}:
            return {"path": str(self.workspace.root_path), "id": "W1", "type": self.workspace.kind}
        if name in {"files", "tree", "projects"}:
            items = self.files.list_files(_int_arg(args, 0, 500))
            self.file_refs.update({item["id"]: item["path"] for item in items})
            return {"files": items}
        if name in {"read", "open", "context", "head", "tail", "goto"}:
            _require(args, 1, "path")
            ref = args[0]
            result_ref = self.results.get(ref)
            args[0] = self._resolve_ref(ref)
            start = _int_arg(args, 1, 1)
            count = _int_arg(args, 2, 120)
            if result_ref:
                line = int(result_ref["line"])
                start = max(1, line - 4) if name in {"context", "open"} else line
                count = 12 if name in {"context", "open"} else count
            if name == "tail":
                info = self.files.read(args[0], 1, 100000)
                info["lines"] = info["lines"][-count:]
                return info
            if name == "head": start = 1
            return self.files.read(args[0], start, count)
        if name in {"search", "find"}:
            _require(args, 1, "query")
            matches = self.searcher.search(args[0], limit=_int_arg(args, 1, 100))
            self.results.update({item["id"]: item for item in matches})
            self.file_refs.update({item["id"]: item["file"] for item in matches})
            return {"matches": matches}
        if name == "new":
            _require(args, 1, "path")
            if self.workspace.resolve(args[0]).exists(): raise FileExistsError(args[0])
            return self._write(args[0], "")
        if name == "mkdir":
            _require(args, 1, "path")
            self.workspace.resolve(args[0]).mkdir(parents=True, exist_ok=True)
            return {"path": args[0], "created": True}
        if name in {"write", "save"}:
            _require(args, 2, "path and content")
            args[0] = self._resolve_ref(args[0])
            return self._write(args[0], " ".join(args[1:]))
        if name == "append":
            _require(args, 2, "path and content")
            args[0] = self._resolve_ref(args[0])
            target = self.workspace.resolve(args[0])
            before = target.read_text(encoding="utf-8") if target.exists() else ""
            addition = " ".join(args[1:])
            separator = "" if not before or before.endswith(("\n", "\r")) else "\n"
            self._write(args[0], before + separator + addition)
            return {"path": args[0], "appended_chars": len(addition)}
        if name == "insert":
            _require(args, 3, "path, line, and text")
            args[0] = self._resolve_ref(args[0])
            return self._edit(args[0], lambda: self.files.insert_line(args[0], int(args[1]), " ".join(args[2:])))
        if name == "replace":
            _require(args, 3, "path, old text, and new text")
            args[0] = self._resolve_ref(args[0])
            return self._edit(args[0], lambda: self.files.replace(args[0], args[1], " ".join(args[2:])))
        if name == "replace-line":
            _require(args, 3, "path, line, and text")
            args[0] = self._resolve_ref(args[0])
            return self._edit(args[0], lambda: self.files.replace_line(args[0], int(args[1]), " ".join(args[2:])))
        if name == "replace-range":
            _require(args, 4, "path, start line, end line, and replacement text")
            args[0] = self._resolve_ref(args[0])
            return self._edit(args[0], lambda: self.files.replace_range(args[0], int(args[1]), int(args[2]), " ".join(args[3:])))
        if name == "delete":
            _require(args, 1, "path")
            args[0] = self._resolve_ref(args[0])
            path = self.workspace.resolve(args[0], must_exist=True)
            if not path.is_file(): raise IsADirectoryError(args[0])
            before = path.read_text(encoding="utf-8")
            path.unlink()
            self._record(args[0], before, None)
            return {"path": args[0], "deleted": True}
        if name == "rename":
            _require(args, 2, "source and destination")
            args[0] = self._resolve_ref(args[0])
            src, dst = self.workspace.resolve(args[0], must_exist=True), self.workspace.resolve(args[1])
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            return {"from": args[0], "to": args[1]}
        if name == "status": return self.git.status()
        if name == "diff": return {"diff": self.git.diff()}
        if name == "begin":
            if self.transaction is not None: raise RuntimeError("A transaction is already active")
            self.transaction = []
            return {"transaction": "TX1", "state": "ACTIVE"}
        if name == "commit":
            if self.transaction is None: raise RuntimeError("No active transaction")
            count = len(self.transaction); self.journal.extend(self.transaction); self.transaction = None
            return {"committed_changes": count}
        if name == "rollback":
            if self.transaction is None: raise RuntimeError("No active transaction")
            changes = list(reversed(self.transaction)); self.transaction = None
            for change in changes: self._restore(change)
            return {"rolled_back": len(changes)}
        if name == "undo":
            if not self.journal: raise RuntimeError("Journal is empty")
            change = self.journal.pop(); self._restore(change)
            return {"undone": change.path}
        if name in {"build", "test", "run"}:
            argv = args or _profile_command(self.workspace, name)
            if not argv: raise RuntimeError(f"No {name} profile detected; pass an explicit command")
            outcome = self.processes.run(argv)
            return outcome
        if name == "set":
            _require(args, 2, "setting and value")
            if args[0] == "output" and args[1] in {"text", "json", "compact"}: self.output = args[1]
            elif args[0] == "transport" and args[1] in {"hid", "local"}: self.output = "compact" if args[1] == "hid" else "text"
            else: raise ValueError("Supported settings: output=text|json|compact, transport=hid|local")
            return {"setting": args[0], "value": args[1]}
        if name == "machine": self.machine = True; return {"machine_mode": True}
        if name == "human": self.machine = False; return {"machine_mode": False}
        if name in {"quit", "exit"}: return {"quit": True}
        raise ValueError(f"Unknown command: {name}")

    def _write(self, path: str, content: str) -> dict:
        resolved = self.workspace.resolve(path)
        before = resolved.read_text(encoding="utf-8") if resolved.exists() else None
        outcome = self.files.write(path, content)
        self._record(path, before, content)
        return outcome

    def _edit(self, path: str, operation) -> dict:
        resolved = self.workspace.resolve(path, must_exist=True)
        before = resolved.read_text(encoding="utf-8")
        outcome = operation()
        after = resolved.read_text(encoding="utf-8")
        self._record(path, before, after)
        return outcome

    def _record(self, path: str, before: str | None, after: str | None) -> None:
        change = Change(path, before, after or "")
        (self.transaction if self.transaction is not None else self.journal).append(change)

    def _restore(self, change: Change) -> None:
        target = self.workspace.resolve(change.path)
        if change.before is None:
            if target.exists(): target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.before, encoding="utf-8")

    def _resolve_ref(self, value: str) -> str:
        return self.file_refs.get(value, value)


def _profile_command(workspace: Workspace, mode: str) -> list[str] | None:
    from easychange.adapters.dotnet import DotnetAdapter
    from easychange.adapters.node import NodeAdapter
    from easychange.adapters.python import PythonAdapter
    for adapter in (DotnetAdapter(), NodeAdapter(), PythonAdapter()):
        if adapter.detect(workspace):
            return adapter.build(workspace) if mode == "build" else adapter.test(workspace) if mode == "test" else None
    return None


def _require(args: list[str], count: int, usage: str) -> None:
    if len(args) < count: raise ValueError(f"Usage requires {usage}")


def _int_arg(args: list[str], index: int, default: int) -> int:
    try: return int(args[index])
    except IndexError: return default


def _error_code(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError): return "FILE_NOT_FOUND"
    if isinstance(exc, PermissionError): return "PATH_OUTSIDE_WORKSPACE"
    if isinstance(exc, LookupError): return "TEXT_NOT_FOUND"
    if isinstance(exc, (ValueError, IndexError)): return "INVALID_ARGUMENT"
    if isinstance(exc, subprocess.TimeoutExpired): return "PROCESS_TIMEOUT"
    return "COMMAND_FAILED"


def _parse_tokens(value: str) -> list[str]:
    lexer = shlex.shlex(value, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    return list(lexer)


import subprocess
