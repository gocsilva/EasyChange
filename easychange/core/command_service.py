from __future__ import annotations

import shlex
import time
import re
import threading
import json
import secrets
import hashlib
from dataclasses import dataclass

from .command_parser import CommandParser
from .file_service import ExternalChangeError, FileService
from .git_service import GitService
from .indexer import Indexer
from .ids import IdFactory
from .journal import Journal
from .locks import LockManager
from .process_service import ProcessService
from .result import Result
from .transport_codec import decode_command
from .state import MachineSession
from .symbol_service import SymbolService
from .workspace import Workspace
from .watcher import WorkspaceWatcher
from ..remote.agent_profile import REMOTE_PROFILE, remote_guide_markdown


@dataclass
class Change:
    path: str
    before: str | None
    after: str | None
    change_id: str | None = None
    command: str = "edit"


class ProcessFailure(RuntimeError):
    def __init__(self, result: Result) -> None:
        super().__init__(result.error or "Process failed")
        self.result = result


class CommandService:
    """Thread-safe control plane shared by CLI, GUI, HTTP, and MCP transports."""
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.state_store = MachineSession(workspace.root_path / ".easychange" / "session.json", str(workspace.root_path))
        self.session_id = self.state_store.data["session_id"]
        self.instance_id = "A" + secrets.token_hex(3).upper()
        self.files = FileService(workspace, self.state_store.data.get("file_hashes", {}))
        self.git = GitService(workspace)
        self.processes = ProcessService(workspace.root_path)
        self.ids = IdFactory(self.state_store.data.get("id_counts"))
        self.output = self.state_store.data.get("output_mode", "text")
        self.machine = self.state_store.data.get("mode", "human") == "machine"
        self.history: list[str] = list(self.state_store.data.get("history", []))[-50:]
        self.aliases = dict(self.state_store.data.get("aliases", {}))
        self.parser = CommandParser(self.aliases)
        self.indexer = Indexer(workspace.root_path)
        self.symbol_service = SymbolService(workspace, self.indexer)
        self.journal_store = Journal(workspace.root_path / ".easychange" / "journal.jsonl", self.session_id)
        self.journal: list[Change] = [Change(e.path, e.before, e.after, e.change_id, e.command) for e in self.journal_store.entries]
        self.locks = LockManager(workspace.root_path / ".easychange" / "locks.json")
        self.transaction_path = workspace.root_path / ".easychange" / "active_transaction.json"
        self.transaction: list[Change] | None = None
        self.transaction_id: str | None = None
        self._load_transaction()
        self.results: dict[str, dict] = dict(self.state_store.data.get("results", {}))
        self.file_refs: dict[str, str] = dict(self.state_store.data.get("file_refs", {}))
        self.page_items: list[dict] = []
        self.page_offset = 0
        self.page_size = 50
        self.page_kind = "files"
        self.page_query: tuple[str, str | None, str | None, bool] | None = None
        self.macros: dict[str, list[str]] = dict(self.state_store.data.get("macros", {}))
        self.snapshots: dict[str, int] = dict(self.state_store.data.get("snapshots", {}))
        self._guard = threading.RLock()
        self.watcher: WorkspaceWatcher | None = None
        self.last_command = ""
        self.last_result: Result | None = None

    def close(self) -> None:
        if self.watcher: self.watcher.stop()
        self.processes.stop_all()
        self.locks.release_owner(self.instance_id, self.session_id)
        self._persist_state(self.last_result)

    def execute(self, command: str) -> Result:
        raw = command.strip()
        if not raw: return Result(False, "", error="Empty command", code="INVALID_COMMAND")
        # EC1 is the physical HID request/response envelope. Journal RUNNING
        # before dispatch so a lost ACK can never cause a blind mutation replay.
        match = re.match(r"^:ec\s+(Q[A-Z0-9_-]{4,40})\s+([\s\S]+)$", raw, re.IGNORECASE)
        if match:
            return self._execute_sequenced(match.group(1).upper(), match.group(2).strip())
        if raw.lower().startswith(":batch"):
            body = raw[len(":batch"):].strip()
            if body.endswith(":end"): body = body[:-4].strip()
            return self._execute_chain(body, batch=True)
        if raw.lower().startswith(":macro define ") and ":end" in raw:
            parts = raw.splitlines()
            header = parts[0]
            body = "\n".join(line for line in parts[1:] if line.strip().lower() != ":end")
            return self.execute_tokens("macro", ["define", *self.parser.parse(header)[2:], body], raw=raw)
        try:
            chain = self.parser.split_chain(raw)
            if len(chain) > 1: return self._execute_chain(raw)
            tokens = self.parser.parse(raw)
            if not tokens:
                raise ValueError("Empty command")
            return self.execute_tokens(tokens[0], tokens[1:], raw=raw)
        except Exception as exc:
            return self._error_result(raw, exc)

    def _execute_sequenced(self, sequence: str, command: str) -> Result:
        if not command:
            return Result(False, "ec", error="Empty sequenced command", code="INVALID_COMMAND", sequence=sequence)
        codec = None
        structured = None
        if command.casefold().startswith(":z1 "):
            try:
                command, codec = decode_command(command)
            except ValueError as exc:
                return Result(False, "ec", error=str(exc), code="INVALID_COMPRESSED_PAYLOAD", sequence=sequence)
        if command.casefold().startswith(":j1 "):
            try:
                structured = json.loads(command[4:])
                if not isinstance(structured, dict):
                    raise ValueError("EC1_J1_OBJECT_REQUIRED")
                command = json.dumps(structured, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            except (json.JSONDecodeError, ValueError) as exc:
                return Result(False, "ec", error=str(exc), code="INVALID_STRUCTURED_PAYLOAD", sequence=sequence)
        digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
        with self._guard:
            journal = dict(self.state_store.data.get("ec_results", {}))
            previous = journal.get(sequence)
            if previous:
                if previous.get("sha256") != digest:
                    return Result(False, "ec", error="Sequence reused with different command", code="SEQUENCE_CONFLICT", sequence=sequence)
                if previous.get("state") != "DONE":
                    return Result(False, "ec", error="Execution outcome is not yet known; inspect state before continuing", code="UNKNOWN", data={"state": previous.get("state", "RUNNING")}, sequence=sequence)
                cached = Result(**previous["result"])
                cached.sequence = sequence
                cached.data = {**cached.data, "duplicate": True}
                return cached
            journal[sequence] = {"sha256": digest, "state": "RUNNING", "command": command[:256]}
            self.state_store.data["ec_results"] = journal
            self._persist_state()

        try:
            result = self._execute_structured(structured) if structured is not None else self.execute(command)
        except Exception as exc:
            result = Result(False, "ec", error="Structured operation outcome is unknown", code="UNKNOWN",
                            data={"exception": type(exc).__name__}, sequence=sequence)
        result.sequence = sequence
        if codec:
            result.data = {**result.data, "transport_codec": codec}
        with self._guard:
            journal = dict(self.state_store.data.get("ec_results", {}))
            journal[sequence] = {"sha256": digest, "state": "DONE", "command": command[:256], "result": result.to_dict()}
            self.state_store.data["ec_results"] = dict(list(journal.items())[-128:])
            self._persist_state(result)
        return result

    def _execute_structured(self, payload: dict) -> Result:
        """Execute bounded HID-delivered operations; read-only batches avoid transactions."""
        started = time.monotonic()
        operation = str(payload.get("op") or "").casefold()
        operations = payload.get("operations") if operation == "batch" else [payload]
        if not isinstance(operations, list) or not 1 <= len(operations) <= 16:
            return Result(False, "structured", error="OPERATION_COUNT_LIMIT", code="INVALID_STRUCTURED_PAYLOAD")

        mutation_types = {"write_file", "create_file", "replace_line"}
        kinds = [str(item.get("type") or "").casefold() if isinstance(item, dict) else "" for item in operations]
        transactional = any(kind in mutation_types for kind in kinds)
        if transactional and self.transaction is not None:
            return Result(False, "structured", error="BATCH_CANNOT_NEST_TRANSACTION", code="TRANSACTION_ACTIVE")

        if transactional:
            opened = self.execute_tokens("begin", [], raw=":begin")
            if not opened.ok:
                return opened

        results = []
        for item in operations:
            if not isinstance(item, dict):
                result = Result(False, "structured", error="OPERATION_OBJECT_REQUIRED", code="INVALID_STRUCTURED_PAYLOAD")
            else:
                kind = str(item.get("type") or "").casefold()
                if kind in {"write_file", "create_file"}:
                    path, content = item.get("path"), item.get("content")
                    if not isinstance(path, str) or not isinstance(content, str):
                        result = Result(False, kind, error="PATH_AND_CONTENT_REQUIRED", code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        try:
                            if kind == "create_file" and self.workspace.resolve(path).exists():
                                result = Result(False, kind, error=f"File already exists: {path}", code="FILE_EXISTS")
                            else:
                                result = self.execute_tokens("write", [path, content], raw=f"EC1 {kind} {path}")
                        except Exception as exc:
                            result = Result(False, kind, error=str(exc), code=getattr(exc, "code", type(exc).__name__))
                elif kind == "replace_line":
                    path, line, content = item.get("path"), item.get("line"), item.get("content")
                    if not isinstance(path, str) or not isinstance(content, str) or isinstance(line, bool) or not isinstance(line, int):
                        result = Result(False, kind, error="PATH_LINE_AND_CONTENT_REQUIRED", code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        result = self.execute_tokens("replace-line", [path, str(line), content], raw=f"EC1 patch {path}:{line}")
                elif kind == "read":
                    path = item.get("path")
                    if not isinstance(path, str):
                        result = Result(False, kind, error="PATH_REQUIRED", code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        args = [path]
                        if isinstance(item.get("start"), int):
                            args.append(str(item["start"]))
                        if isinstance(item.get("count"), int):
                            args.append(str(item["count"]))
                        result = self.execute_tokens("read", args, raw=f"EC1 read {path}")
                elif kind in {"search", "locate", "study", "definition", "references"}:
                    query = item.get("query") or item.get("name")
                    if not isinstance(query, str) or not query:
                        result = Result(False, kind, error="QUERY_REQUIRED", code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        args = [query]
                        if kind in {"search", "locate", "study"} and isinstance(item.get("limit"), int):
                            args += ["--limit", str(item["limit"])]
                        if kind in {"locate", "study"} and isinstance(item.get("context"), int):
                            args += ["--context", str(item["context"])]
                        result = self.execute_tokens(kind, args, raw=f"EC1 {kind} {query}")
                elif kind == "read_many":
                    paths = item.get("paths")
                    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                        result = Result(False, kind, error="PATHS_REQUIRED", code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        args = list(paths)
                        if isinstance(item.get("count"), int):
                            args += ["--count", str(item["count"])]
                        result = self.execute_tokens("read-many", args, raw="EC1 read-many")
                elif kind == "validate":
                    args = ["--test"] if item.get("test") else []
                    result = self.execute_tokens("validate", args, raw="EC1 validate")
                else:
                    result = Result(False, "structured", error=f"Unsupported operation: {kind}", code="OPERATION_NOT_ALLOWED")
            results.append(result.to_dict())
            if not result.ok:
                break

        ok = len(results) == len(operations) and all(item["ok"] for item in results)
        transaction_data = {"state": "NOT_REQUIRED"}
        if transactional:
            transaction = self.execute_tokens("commit" if ok else "rollback", [], raw=":commit" if ok else ":rollback")
            transaction_data = transaction.data if transaction.ok else {"state": "UNKNOWN", "error": transaction.code}
            ok = bool(ok and transaction.ok)

        result = Result(
            ok,
            "structured",
            data={"results": results, "count": len(results), "transaction": transaction_data},
            error=None if ok else ("Structured batch failed and was rolled back" if transactional else "Structured read batch failed"),
            code=None if ok else "STRUCTURED_BATCH_FAILED",
            command_id=self.ids.next("C"),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        self._persist_state(result)
        return result

    def _execute_chain(self, text: str, batch: bool = False) -> Result:
        started = time.monotonic()
        body = text
        if batch and body.lower().startswith(":batch"): body = body[len(":batch"):].strip()
        if ":end" in body: body = body.rsplit(":end", 1)[0]
        if batch and self.transaction is not None:
            return self._error_result(":batch", RuntimeError("BATCH_CANNOT_NEST_TRANSACTION"))
        if batch:
            opened = self.execute_tokens("begin", [], raw=":begin")
            if not opened.ok:
                return opened
        try:
            commands = self.parser.split_chain(body.replace("\r", "\n").replace("\n", ";"))
            results = []
            previous_stop = False
            for item, stop_on_error in commands:
                if previous_stop and results and not results[-1]["ok"]: break
                tokens = self.parser.parse(item)
                if not tokens: continue
                result = self.execute_tokens(tokens[0], tokens[1:], raw=item)
                results.append(result.to_dict())
                previous_stop = stop_on_error
                if batch and not result.ok:
                    break
            ok = all(item["ok"] for item in results)
            transaction_result = None
            if batch:
                transaction_result = self.execute_tokens("commit" if ok else "rollback", [], raw=":commit" if ok else ":rollback")
                ok = bool(ok and (transaction_result is None or transaction_result.ok))
            result = Result(ok, "batch" if batch else "chain", data={"results": results, "count": len(results)},
                            command_id=self.ids.next("C"), duration_ms=int((time.monotonic()-started)*1000))
            if batch:
                result.data["transaction"] = transaction_result.data if transaction_result and transaction_result.ok else {
                    "state": "UNKNOWN", "error": transaction_result.code if transaction_result else "TRANSACTION_FINALIZE_FAILED"}
            self._persist_state(result)
            return result
        except Exception as exc:
            if batch and self.transaction is not None:
                try:
                    self.execute_tokens("rollback", [], raw=":rollback")
                except Exception:
                    pass
            return self._error_result(":batch" if batch else text, exc)

    def _error_result(self, raw: str, exc: Exception) -> Result:
        if isinstance(exc, ProcessFailure):
            result = exc.result
            result.command_id = self.ids.next("C")
            self._persist_state(result)
            return result
        data = {}
        if isinstance(exc, ExternalChangeError):
            data = {"file": exc.path, "expected_hash": exc.expected, "actual_hash": exc.actual,
                    "options": ["reload", "diff", "force-write"]}
        result = Result(False, raw.split(maxsplit=1)[0].lstrip(":").lower(), error=str(exc),
                        code=_error_code(exc), command_id=self.ids.next("C"), data=data)
        self._persist_state(result)
        return result

    def execute_tokens(self, name: str, args: list[str], *, raw: str | None = None) -> Result:
        """Execute already-tokenized input for transports such as MCP."""
        started = time.monotonic()
        name = name.lower()
        if name in self.aliases: name = self.aliases[name]
        self.history.append(raw if raw is not None else ":" + " ".join([name, *args]))
        self.last_command = name
        try:
            with self._guard:
                data = self._dispatch(name, list(args))
            result = Result(True, name, data=data, command_id=self.ids.next("C"))
        except Exception as exc:
            result = self._error_result(name, exc)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        self.last_command = name
        self.last_result = result
        self._persist_state(result)
        return result

    def _persist_state(self, result: Result | None = None) -> None:
        self.state_store.data.update({"session_id": self.session_id, "workspace": str(self.workspace.root_path),
            "current_file": self.state_store.data.get("current_file"), "output_mode": self.output,
            "transport": self.state_store.data.get("transport", "local"), "mode": "machine" if self.machine else "human",
            "history": self.history[-50:], "id_counts": self.ids.counts(), "aliases": self.aliases,
            "macros": self.macros, "snapshots": self.snapshots,
            "last_command": self.last_command, "last_result": result.to_dict() if result else None,
            "file_refs": self.file_refs, "results": self.results})
        self.state_store.data["file_hashes"] = self.files.baselines
        self.state_store.save()

    def _dispatch(self, name: str, args: list[str]) -> dict:
        if name in {"help", "capabilities"}:
            return {"commands": ["state", "workspace", "pwd", "files", "tree", "next", "prev", "read", "head", "tail", "context", "goto",
                    "search", "find", "index", "symbols", "symbol", "definition", "references", "implementations", "outline", "file-summary",
                    "locate", "study", "read-many", "validate", "edit-result",
                    "new", "mkdir", "write", "append", "insert", "replace", "replace-line", "replace-range", "delete", "rename", "move", "stat", "hash", "exists",
                    "rename-symbol", "create-class", "create-interface", "create-test", "format", "fix-imports", "organize-imports",
                    "journal", "undo", "redo", "begin", "commit", "rollback", "snapshot", "lock", "unlock", "locks", "watch", "batch", "macro", "alias", "complete", "history", "repeat", "clear",
                    "status", "diff", "branch", "log", "build", "test", "run", "processes", "stop", "errors", "next-error", "previous-error", "remote", "remote-guide", "remote-profile", "prepare-hid", "set", "machine", "human", "quit"],
                    "capabilities": {"workspace": True, "git": "GIT" in self.workspace.adapters,
                    "build_test": self.workspace.adapters, "index": True, "symbols": True,
                    "ai_machine": {"optical_protocol": "EC2", "qr_slots": 4, "chunked_results": True,
                                   "composite_study": True, "read_many": True, "validate": True},
                    "remote_control": {"input": "ESP32_HID", "output": "HDMI",
                                       "mcp_on_remote_pc": False, "api_on_remote_pc": False}}}
        if name == "state":
            git = self.git.status() if "GIT" in self.workspace.adapters else None
            return {"state": "READY", "workspace": str(self.workspace.root_path), "workspace_id": "W1",
                    "instance_id": self.instance_id,
                    "name": self.workspace.name, "type": self.workspace.kind, "adapters": self.workspace.adapters,
                    "file": self.state_store.data.get("current_file"), "line": self.state_store.data.get("line", 1),
                    "dirty": self.state_store.data.get("dirty", False), "git": git,
                    "transaction": self.transaction_id or "NONE", "process": self.processes.list(),
                    "index": "READY" if (self.workspace.root_path / ".easychange" / "index.db").exists() else "NOT_BUILT",
                    "machine_mode": self.machine, "focus": "COMMAND"}
        if name in {"pwd", "workspace"}:
            return {"path": str(self.workspace.root_path), "id": "W1", "type": self.workspace.kind}
        if name in {"files", "tree", "projects"}:
            offset, limit = _page_args(args, default_limit=self.page_size)
            items = self.indexer.files(offset=offset, limit=limit)
            items = [{"path": item["path"], "id": self.ids.next("F"), "kind": item["language"],
                      "size": item["size"], "hash": item["hash"][:12]} for i, item in enumerate(items)]
            self.page_items = items; self.page_offset = offset; self.page_size = limit
            self.file_refs.update({item["id"]: item["path"] for item in items})
            total = self.indexer.count()
            return {"page": offset // limit + 1, "pages": max(1, (total + limit - 1) // limit), "total": total, "files": items}
        if name in {"next", "prev"}:
            offset = max(0, self.page_offset + (self.page_size if name == "next" else -self.page_size))
            if self.page_kind == "search" and self.page_query:
                query, extension, path_prefix, regex = self.page_query
                matches = self.indexer.search(query, extension=extension, path_prefix=path_prefix, regex=regex,
                                              offset=offset, limit=self.page_size)
                matches = self._register_matches(matches)
                self.page_offset = offset; self.page_items = matches
                return {"page": offset // self.page_size + 1, "matches": matches}
            return self._dispatch("files", ["--offset", str(offset), "--limit", str(self.page_size)])
        if name in {"read", "open", "context", "head", "tail", "goto"}:
            if name == "context" and args and args[0].isdigit():
                current = self.state_store.data.get("current_file")
                if not current: raise ValueError("No current file; use :context <path> <line> [count]")
                args.insert(0, current)
            if name == "goto" and args and args[0].isdigit():
                current = self.state_store.data.get("current_file")
                if not current: raise ValueError("No current file; use :goto <path> <line>")
                args.insert(0, current)
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
                self._set_current(args[0], max(1, info["total_lines"]-count+1))
                return info
            if name == "head": start = 1
            if name == "goto":
                line = int(result_ref["line"]) if result_ref else max(1, _int_arg(args, 1, 1))
                self._set_current(args[0], line)
                return {"file": args[0], "line": line}
            info = self.files.read(args[0], start, count)
            self._set_current(args[0], start)
            return info
        if name in {"search", "find", "locate"}:
            _require(args, 1, "query")
            query, extension, path_prefix, regex = _search_args(args)
            offset, limit = _page_args(args, default_limit=10 if name == "locate" else self.page_size)
            matches = self.indexer.search(query, extension=extension, path_prefix=path_prefix, regex=regex,
                                          offset=offset, limit=limit)
            matches = self._register_matches(matches)
            self.page_items = matches; self.page_offset = offset; self.page_size = limit
            self.page_kind = "search"; self.page_query = (query, extension, path_prefix, regex)
            if name == "locate":
                radius = _option_int(args, "--context", 2, minimum=0, maximum=20)
                for match in matches:
                    start = max(1, int(match["line"]) - radius)
                    match["context"] = self.files.read(match["file"], start, radius * 2 + 1)["lines"]
                return {"query": query, "count": len(matches), "matches": matches}
            return {"page": offset // limit + 1, "matches": matches}

        if name == "study":
            _require(args, 1, "symbol or search term")
            term = args[0]
            limit = _option_int(args, "--limit", 8, minimum=1, maximum=20)
            radius = _option_int(args, "--context", 3, minimum=0, maximum=20)
            matches = self._register_matches(self.indexer.search(term, limit=limit))
            for match in matches:
                start = max(1, int(match["line"]) - radius)
                match["context"] = self.files.read(match["file"], start, radius * 2 + 1)["lines"]
            definitions = self.symbol_service.definition(term)
            references = self._register_matches(self.symbol_service.references(term, min(24, limit * 3)))
            files = []
            seen = set()
            for item in [*definitions, *matches, *references]:
                path = item.get("file")
                if path and path not in seen:
                    seen.add(path); files.append(path)
            return {
                "term": term,
                "definitions": definitions[:8],
                "matches": matches,
                "references": references,
                "files": files[:16],
                "index": "incremental",
            }

        if name == "read-many":
            _require(args, 1, "one or more paths")
            count = _option_int(args, "--count", 120, minimum=1, maximum=600)
            paths = []
            skip = False
            for index, item in enumerate(args):
                if skip:
                    skip = False
                    continue
                if item == "--count":
                    skip = True
                    continue
                paths.append(self._resolve_ref(item))
            if not paths or len(paths) > 8:
                raise ValueError("read-many requires 1 to 8 paths")
            return {"files": [self.files.read(path, 1, count) for path in paths], "count": len(paths)}

        if name == "edit-result":
            _require(args, 2, "search result ID and replacement line")
            match = self.results.get(args[0])
            if match is None: raise LookupError(f"Search result not found: {args[0]}")
            path = match["file"]
            line = int(match["line"])
            replacement = " ".join(args[1:])
            outcome = self._edit(path, lambda: self.files.replace_line(path, line, replacement))
            return {"result_id": args[0], "file": path, "line": line, "edit": outcome}
        if name == "index": return self.indexer.refresh()
        if name in {"symbols", "symbol", "definition", "references", "implementations", "outline"}:
            if name != "symbols": _require(args, 1, "symbol name or file path")
            target = self._resolve_ref(args[0]) if args else None
            if name == "references":
                matches = self.symbol_service.references(target, _int_arg(args, 1, 100))
                return {"references": self._register_matches(matches)}
            if name == "implementations":
                return {"implementations": [item for item in self.symbol_service.definition(target)
                        if item["kind"] in {"class", "interface", "method", "function"}]}
            if name in {"definition", "symbol"}: return {"symbols": self.symbol_service.definition(target)}
            symbols = self.symbol_service.symbols(target if name == "outline" else None)
            if name == "outline": return {"outline": symbols}
            return {"symbols": symbols}
        if name == "file-summary":
            _require(args, 1, "file path")
            path = self._resolve_ref(args[0]); metadata = self.files.info(path)
            if metadata["kind"] != "text": return {"file": metadata, "summary": "non-text file"}
            content = self.files.read(path, 1, 40)
            return {"file": metadata, "preview": content["lines"], "symbols": self.symbol_service.symbols(path)}
        if name == "complete":
            prefix = args[0] if args else ":"
            if prefix.startswith(":open ") or prefix.startswith(":read "):
                part = prefix.split(maxsplit=1)[1]
                candidates = [item["path"] for item in self.indexer.files(limit=1000) if item["path"].startswith(part)]
            else:
                candidates = [":" + command for command in self._commands() if (":" + command).startswith(prefix)]
            return {"candidates": candidates[:50]}
        if name == "alias":
            _require(args, 2, "alias and target command")
            self.aliases[args[0]] = args[1]; self.parser.aliases[args[0]] = args[1]
            return {"alias": args[0], "command": args[1]}
        if name == "macro": return self._macro(args)
        if name == "history":
            offset, limit = _page_args(args, default_limit=50)
            return {"history": self.history[-(offset+limit):-offset if offset else None]}
        if name == "clear":
            self.results.clear(); self.file_refs.clear(); self.page_items = []; self.page_offset = 0
            return {"cleared": ["results", "file_refs", "page"]}
        if name == "repeat":
            _require(args, 1, "history index or last")
            index = -2 if args[0] == "last" else int(args[0]) - 1
            if not self.history: raise LookupError("Command history is empty")
            return self.execute(self.history[index]).data
        if name in {"stat", "hash", "exists"}:
            _require(args, 1, "path")
            path = self._resolve_ref(args[0])
            if name == "exists": return {"path": path, "exists": self.files.exists(path)}
            if name == "hash": return {"path": path, "hash": self.files.hash(path)}
            return self.files.info(path)
        if name == "reload":
            _require(args, 1, "path")
            path = self._resolve_ref(args[0]); self.files.remember(path)
            return self.files.read(path, _int_arg(args, 1, 1), _int_arg(args, 2, 120))
        if name == "force-write":
            _require(args, 2, "path and content")
            path = self._resolve_ref(args[0]); return self._force_write(path, " ".join(args[1:]))
        if name == "rename-symbol":
            _require(args, 2, "old symbol name and new symbol name")
            old, new = args[0], args[1]
            if not re.fullmatch(r"[A-Za-z_$][\w$]*", old) or not re.fullmatch(r"[A-Za-z_$][\w$]*", new):
                raise ValueError("Symbol names must be identifiers")
            if not self.symbol_service.definition(old): raise LookupError(f"Symbol definition not found: {old}")
            candidates = self.indexer.files(limit=100000)
            changes = []
            own_transaction = self.transaction is None
            if own_transaction:
                self._dispatch("begin", [])
            try:
                pattern = re.compile(rf"\b{re.escape(old)}\b")
                for item in candidates:
                    if item["size"] > 2 * 1024 * 1024 or item["language"] == "text":
                        continue
                    path = item["path"]
                    target = self.workspace.resolve(path, must_exist=True)
                    try: content = target.read_text(encoding="utf-8-sig")
                    except (OSError, UnicodeDecodeError): continue
                    if not pattern.search(content): continue
                    count = len(pattern.findall(content))
                    self._edit(path, lambda p=path, c=content: self.files.write(p, pattern.sub(new, c)))
                    changes.append({"file": path, "replacements": count})
                if not changes:
                    if own_transaction: self._dispatch("rollback", [])
                    raise LookupError(f"Symbol not found: {old}")
                if own_transaction: self._dispatch("commit", [])
            except Exception:
                if own_transaction and self.transaction is not None:
                    self._dispatch("rollback", [])
                raise
            return {"renamed": old, "to": new, "files_changed": len(changes), "replacements": sum(x["replacements"] for x in changes), "files": changes}
        if name in {"create-class", "create-interface", "create-test"}:
            _require(args, 1, "name [path]")
            symbol_name = args[0]
            if not re.fullmatch(r"[A-Za-z_$][\w$]*", symbol_name): raise ValueError("Name must be an identifier")
            path = args[1] if len(args) > 1 else _scaffold_path(name, symbol_name)
            target = self.workspace.resolve(path)
            if target.exists(): raise FileExistsError(path)
            content = _scaffold_content(name, symbol_name, target.suffix.lower())
            return self._write(path, content)
        if name in {"format", "fix-imports", "organize-imports"}:
            raise NotImplementedError(f"{name} requires a language adapter; none is configured for this workspace")
        if name == "journal":
            offset, limit = _page_args(args, default_limit=50)
            entries = self.journal_store.entries
            return {"total": len(entries), "entries": [entry.__dict__ if hasattr(entry, "__dict__") else {
                    "change_id": entry.change_id, "timestamp": entry.timestamp, "command": entry.command,
                    "path": entry.path, "before_hash": entry.before_hash, "after_hash": entry.after_hash,
                    "transaction_id": entry.transaction_id, "source": entry.source}
                    for entry in entries[offset:offset+limit]]}
        if name == "snapshot": return self._snapshot(args)
        if name == "lock":
            _require(args, 1, "file ID or path")
            path = self._resolve_ref(args[0]); relative = self.workspace.resolve(path).relative_to(self.workspace.root_path).as_posix()
            lock = self.locks.acquire(relative, self.instance_id, self.session_id, _int_arg(args, 1, 3600) or None)
            return {"locked": lock.path, "owner": lock.owner, "expires_at": lock.expires_at}
        if name == "unlock":
            _require(args, 1, "file ID or path")
            path = self.workspace.resolve(self._resolve_ref(args[0])).relative_to(self.workspace.root_path).as_posix()
            return {"unlocked": self.locks.release(path, self.instance_id, self.session_id), "path": path}
        if name == "locks": return {"locks": self.locks.list()}
        if name == "watch": return self._watch(args)
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
            relative = path.relative_to(self.workspace.root_path).as_posix()
            self.files.remember(relative); self.locks.check_write(relative, self.instance_id, self.session_id)
            before = path.read_text(encoding="utf-8")
            self.files.delete(relative)
            self._record(relative, before, None)
            return {"path": args[0], "deleted": True}
        if name == "rename":
            _require(args, 2, "source and destination")
            args[0] = self._resolve_ref(args[0])
            src, dst = self.workspace.resolve(args[0], must_exist=True), self.workspace.resolve(args[1])
            old_relative = src.relative_to(self.workspace.root_path).as_posix()
            new_relative = dst.relative_to(self.workspace.root_path).as_posix()
            self.files.remember(old_relative)
            self.locks.check_write(old_relative, self.instance_id, self.session_id)
            self.locks.check_write(new_relative, self.instance_id, self.session_id)
            content = src.read_text(encoding="utf-8")
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            self._record(old_relative, content, None)
            self._record(new_relative, None, content)
            self.files.forget(old_relative); self.files.remember(new_relative)
            return {"from": args[0], "to": args[1]}
        if name == "move": return self._dispatch("rename", args)
        if name == "git":
            _require(args, 1, "status, diff, branch, or log")
            if args[0] not in {"status", "diff", "branch", "log"}: raise ValueError("Supported git commands: status, diff, branch, log")
            return self._dispatch(args[0], args[1:])
        if name in {"status", "diff", "branch", "log"} and "GIT" not in self.workspace.adapters:
            raise NotImplementedError("Git is unavailable because this workspace is not inside a Git repository")
        if name == "status": return self.git.status()
        if name == "diff":
            path = self._resolve_ref(args[0]) if args else None
            return {"diff": self.git.diff(path)}
        if name == "branch": return {"branch": self.git.branch()}
        if name == "log": return {"commits": self.git.log(_int_arg(args, 0, 10))}
        if name == "begin":
            if self.transaction is not None: raise RuntimeError("A transaction is already active")
            self.transaction = []
            self.transaction_id = self.ids.next("TX")
            self._save_transaction()
            self._persist_state()
            return {"transaction": self.transaction_id, "state": "ACTIVE"}
        if name == "commit":
            if self.transaction is None: raise RuntimeError("No active transaction")
            count = len(self.transaction)
            for change in self.transaction:
                entry = self.journal_store.record(change.command, change.path, change.before, change.after, self.transaction_id)
                change.change_id = entry.change_id
                self.journal.append(change)
            self.transaction = None; self.transaction_id = None
            self._clear_transaction()
            self._persist_state()
            return {"committed_changes": count}
        if name == "rollback":
            if self.transaction is None: raise RuntimeError("No active transaction")
            changes = list(reversed(self.transaction))
            for change in changes: self._restore(change)
            self.transaction = None; self.transaction_id = None
            self._clear_transaction()
            self._persist_state()
            return {"rolled_back": len(changes)}
        if name == "undo":
            entry_id = args[0] if args else None
            entries = self.journal_store.entries
            entry = next((item for item in entries if item.change_id == entry_id), None) if entry_id else (entries[-1] if entries else None)
            if entry is None: raise RuntimeError("Journal is empty" if not entry_id else f"Journal entry not found: {entry_id}")
            change = Change(entry.path, entry.before, entry.after, entry.change_id, entry.command)
            self._restore(change)
            self.journal_store.undo(entry_id)
            self.journal = [item for item in self.journal if item.change_id != change.change_id]
            return {"undone": change.path, "change_id": change.change_id}
        if name == "redo":
            redone = self.journal_store.redo()
            if not redone: raise RuntimeError("Redo journal is empty")
            entry = redone[0]; change = Change(entry.path, entry.before, entry.after, entry.change_id, entry.command)
            self._apply_after(change); self.journal.append(change)
            return {"redone": change.path, "change_id": change.change_id}
        if name == "validate":
            include_test = "--test" in args or "test" in args
            diff = self.git.diff() if "GIT" in self.workspace.adapters else ""
            build = self.execute_tokens("build", [], raw=":build")
            if not build.ok:
                raise ProcessFailure(Result(False, "validate", data={"diff": diff, "build": build.to_dict(), "test": None},
                                            error=build.error, code=build.code or "BUILD_FAILED"))
            test_result = None
            if include_test:
                test_result = self.execute_tokens("test", [], raw=":test")
                if not test_result.ok:
                    raise ProcessFailure(Result(False, "validate", data={"diff": diff, "build": build.to_dict(),
                                                                          "test": test_result.to_dict()},
                                                error=test_result.error, code=test_result.code or "TEST_FAILED"))
            return {"passed": True, "diff": diff, "build": build.to_dict(),
                    "test": test_result.to_dict() if test_result else None}

        if name in {"build", "test", "run"}:
            if name == "run" and args and args[0] == "--background":
                process_id = self.ids.next("P")
                return self.processes.start(args[1:], process_id)
            argv = args or _profile_command(self.workspace, name)
            if not argv: raise RuntimeError(f"No {name} profile detected; pass an explicit command")
            outcome = self.processes.run(argv)
            if outcome["returncode"]:
                result = Result(False, name, data=outcome, error=outcome["stderr"] or f"Process exited {outcome['returncode']}",
                                code="BUILD_FAILED" if name == "build" else "TEST_FAILED" if name == "test" else "PROCESS_FAILED")
                raise ProcessFailure(result)
            return outcome
        if name == "processes": return {"processes": self.processes.list()}
        if name == "stop":
            _require(args, 1, "process ID")
            return self.processes.stop(args[0])
        if name == "errors":
            outcome = self.processes.last_execution or {}
            lines = (outcome.get("stderr", "") + "\n" + outcome.get("stdout", "")).splitlines()
            matches = []
            for number, line in enumerate(lines, 1):
                if re.search(r"error|exception|failed|traceback", line, re.I):
                    matches.append({"id": f"E{len(matches)+1}", "line": number, "text": line})
            self.page_items = matches; self.page_offset = 0
            return {"errors": matches, "count": len(matches)}
        if name in {"next-error", "previous-error"}:
            errors = self._dispatch("errors", []).get("errors", [])
            if not errors: return {"error": None, "count": 0}
            current = int(self.state_store.data.get("error_index", -1))
            current = min(len(errors)-1, current+1) if name == "next-error" else max(0, current-1)
            self.state_store.data["error_index"] = current
            return {"error": errors[current], "index": current+1, "count": len(errors)}
        if name == "set":
            _require(args, 2, "setting and value")
            if args[0] == "output" and args[1] in {"text", "json", "compact"}: self.output = args[1]
            elif args[0] == "verbosity" and args[1] in {"verbose", "normal", "compact", "hid"}: self.output = "compact" if args[1] == "hid" else "text"
            elif args[0] == "transport" and args[1] in {"hid", "esp_hid", "local"}:
                self.output = "compact" if args[1] in {"hid", "esp_hid"} else "text"
                self.state_store.data["transport"] = args[1]
            else: raise ValueError("Supported settings: output=text|json|compact, transport=hid|local")
            self._persist_state()
            return {"setting": args[0], "value": args[1]}
        if name == "remote":
            return {"ready": True, "workspace": str(self.workspace.root_path),
                    "mode": "MACHINE" if self.machine else "HUMAN",
                    "transport": self.state_store.data.get("transport", "local").upper(), "output": self.output.upper(),
                    "next": [":state", ":capabilities", ":remote-guide"],
                    "workflow": REMOTE_PROFILE["workflow"], "command_focus": "Ctrl+K", "submit": "Enter"}
        if name == "remote-guide": return {"guide": remote_guide_markdown()}
        if name == "remote-profile": return REMOTE_PROFILE
        if name == "prepare-hid":
            self.machine = True; self.output = "compact"
            self.state_store.data["transport"] = "hid"
            self._persist_state()
            return {"ready": True, "machine_mode": True, "transport": "hid", "workspace": str(self.workspace.root_path),
                    "next": ":state", "focus": "Ctrl+K"}
        if name == "machine": self.machine = True; self._persist_state(); return {"machine_mode": True, "focus": "COMMAND"}
        if name == "human": self.machine = False; self._persist_state(); return {"machine_mode": False}
        if name in {"quit", "exit"}: return {"quit": True}
        raise ValueError(f"Unknown command: {name}")

    def _write(self, path: str, content: str) -> dict:
        resolved = self.workspace.resolve(path)
        relative = resolved.relative_to(self.workspace.root_path).as_posix()
        self.locks.check_write(relative, self.instance_id, self.session_id)
        if resolved.exists(): self.files.remember(relative)
        before = resolved.read_text(encoding="utf-8") if resolved.exists() else None
        outcome = self.files.write(path, content)
        self._record(relative, before, content)
        self.indexer.update_path(relative)
        return outcome

    def _force_write(self, path: str, content: str) -> dict:
        resolved = self.workspace.resolve(path)
        relative = resolved.relative_to(self.workspace.root_path).as_posix()
        self.locks.check_write(relative, self.instance_id, self.session_id)
        before = resolved.read_text(encoding="utf-8") if resolved.exists() else None
        if resolved.exists(): self.files.remember(relative)
        outcome = self.files.write(path, content, force=True)
        self._record(relative, before, content)
        self.indexer.update_path(relative)
        return outcome

    def _edit(self, path: str, operation) -> dict:
        resolved = self.workspace.resolve(path, must_exist=True)
        relative = resolved.relative_to(self.workspace.root_path).as_posix()
        self.locks.check_write(relative, self.instance_id, self.session_id)
        self.files.remember(relative)
        before = resolved.read_text(encoding="utf-8")
        outcome = operation()
        after = resolved.read_text(encoding="utf-8")
        self._record(relative, before, after)
        self.indexer.update_path(relative)
        return outcome

    def _record(self, path: str, before: str | None, after: str | None) -> None:
        change = Change(path, before, after, command=self.last_command or "edit")
        if self.transaction is not None:
            self.transaction.append(change)
            self._save_transaction()
        else:
            entry = self.journal_store.record(change.command, path, before, after)
            change.change_id = entry.change_id
            self.journal.append(change)

    def _restore(self, change: Change) -> None:
        target = self.workspace.resolve(change.path)
        self.locks.check_write(change.path, self.instance_id, self.session_id)
        if target.exists():
            actual = self.files.hash(change.path)
            expected = self._hash_content(change.after)
            if expected is None or actual != expected:
                raise ExternalChangeError(change.path, expected or "<absent>", actual)
        elif change.after is not None:
            raise ExternalChangeError(change.path, self._hash_content(change.after) or "", "<absent>")
        if change.before is None:
            if target.exists(): self.files.delete(change.path)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.files.remember(change.path, replace=True) if target.exists() else None
            self.files.write(change.path, change.before)

    def _apply_after(self, change: Change) -> None:
        target = self.workspace.resolve(change.path)
        self.locks.check_write(change.path, self.instance_id, self.session_id)
        if change.before is None:
            if target.exists(): raise ExternalChangeError(change.path, "<absent>", self.files.hash(change.path))
        elif not target.exists() or self.files.hash(change.path) != self._hash_content(change.before):
            raise ExternalChangeError(change.path, self._hash_content(change.before) or "", "<changed>")
        if change.after is None:
            if target.exists(): self.files.delete(change.path)
        else:
            if target.exists(): self.files.remember(change.path, replace=True)
            self.files.write(change.path, change.after)

    def _load_transaction(self) -> None:
        if not self.transaction_path.exists(): return
        try:
            value = json.loads(self.transaction_path.read_text(encoding="utf-8"))
            self.transaction_id = value["transaction_id"]
            self.transaction = [Change(**item) for item in value.get("changes", [])]
        except (OSError, ValueError, KeyError, TypeError):
            self.transaction_id = None
            self.transaction = None

    def _save_transaction(self) -> None:
        if self.transaction is None: return
        self.transaction_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.transaction_path.with_suffix(".tmp")
        value = {"transaction_id": self.transaction_id,
                 "changes": [{"path": change.path, "before": change.before, "after": change.after,
                              "change_id": change.change_id, "command": change.command} for change in self.transaction]}
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.transaction_path)

    def _clear_transaction(self) -> None:
        self.transaction_path.unlink(missing_ok=True)

    @staticmethod
    def _hash_content(content: str | None) -> str | None:
        from .journal import content_hash
        return content_hash(content)

    def _set_current(self, path: str, line: int = 1) -> None:
        relative = self.workspace.resolve(path, must_exist=True).relative_to(self.workspace.root_path).as_posix()
        self.state_store.data["current_file"] = relative
        self.state_store.data["line"] = max(1, line)

    def _snapshot(self, args: list[str]) -> dict:
        _require(args, 1, "create or restore [snapshot ID]")
        if args[0] == "create":
            if self.transaction is not None: raise RuntimeError("Commit or rollback the active transaction before snapshot")
            snapshot_id = self.ids.next("SN")
            self.snapshots[snapshot_id] = len(self.journal_store.entries)
            self._persist_state()
            return {"snapshot": snapshot_id, "changes": len(self.journal_store.entries)}
        if args[0] == "restore":
            _require(args, 2, "restore and snapshot ID")
            snapshot_id = args[1]
            if snapshot_id not in self.snapshots: raise LookupError(f"Snapshot not found: {snapshot_id}")
            entries = self.journal_store.entries[self.snapshots[snapshot_id]:]
            for entry in reversed(entries):
                self._restore(Change(entry.path, entry.before, entry.after, entry.change_id, entry.command))
                self.journal_store.undo(entry.change_id)
                self.journal = [item for item in self.journal if item.change_id != entry.change_id]
            return {"restored": snapshot_id, "changes": len(entries)}
        raise ValueError("Usage: :snapshot create | :snapshot restore SN1")

    def _macro(self, args: list[str]) -> dict:
        _require(args, 1, "define, run, or list")
        if args[0] == "list": return {"macros": self.macros}
        if args[0] == "define":
            _require(args, 3, "define name and command body")
            name = args[1]
            commands = [part.strip() for part, _ in self.parser.split_chain(args[2].replace("\n", ";")) if part.strip()]
            self.macros[name] = commands
            self._persist_state()
            return {"macro": name, "commands": len(commands)}
        if args[0] == "run":
            _require(args, 2, "run name and arguments")
            name, values = args[1], args[2:]
            if name not in self.macros: raise LookupError(f"Macro not found: {name}")
            results = []
            for command in self.macros[name]:
                for index, value in enumerate(values, 1): command = command.replace(f"${index}", value)
                result = self.execute(command)
                results.append(result.to_dict())
                if not result.ok: break
            return {"macro": name, "results": results}
        raise ValueError("Usage: :macro define|run|list ...")

    def _watch(self, args: list[str]) -> dict:
        action = args[0] if args else "status"
        if action == "start":
            if self.watcher is None: self.watcher = WorkspaceWatcher(self.indexer)
            self.watcher.start(); return {"watcher": "RUNNING"}
        if action == "stop":
            if self.watcher: self.watcher.stop()
            return {"watcher": "STOPPED"}
        if action == "poll": return self.watcher.poll_once() if self.watcher else self.indexer.refresh()
        return {"watcher": "RUNNING" if self.watcher and self.watcher._thread and self.watcher._thread.is_alive() else "STOPPED",
                "last_change": self.watcher.last_change if self.watcher else None,
                "last_error": self.watcher.last_error if self.watcher else None}

    def _commands(self) -> list[str]:
        return self._dispatch("capabilities", []).get("commands", [])

    def _resolve_ref(self, value: str) -> str:
        return self.file_refs.get(value, value)

    def _register_matches(self, matches: list[dict]) -> list[dict]:
        registered = []
        for value in matches:
            item = dict(value)
            item["id"] = self.ids.next("R")
            self.results[item["id"]] = item
            if item.get("file"):
                self.file_refs[item["id"]] = item["file"]
            registered.append(item)
        return registered


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


def _page_args(args: list[str], default_limit: int = 50) -> tuple[int, int]:
    offset, limit = 0, default_limit
    for index, value in enumerate(args[:-1]):
        if value == "--offset": offset = max(0, int(args[index + 1]))
        elif value == "--limit": limit = max(1, min(500, int(args[index + 1])))
    if args and args[0].isdigit(): limit = max(1, min(500, int(args[0])))
    return offset, limit


def _option_int(args: list[str], name: str, default: int, *, minimum: int, maximum: int) -> int:
    for index, value in enumerate(args[:-1]):
        if value == name:
            parsed = int(args[index + 1])
            return max(minimum, min(maximum, parsed))
    return default


def _search_args(args: list[str]) -> tuple[str, str | None, str | None, bool]:
    query_parts = []
    extension = path_prefix = None
    regex = False
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"--offset", "--limit", "--context"}:
            index += 2
            continue
        if value.startswith("--"):
            index += 1
            continue
        if value.startswith("ext:"): extension = value[4:] if value[4:].startswith(".") else "." + value[4:]
        elif value.startswith("path:"): path_prefix = value[5:]
        elif value.startswith("regex:"):
            regex = True; query_parts.append(value[6:])
        else: query_parts.append(value)
        index += 1
    query = " ".join(query_parts)
    if not query: raise ValueError("Search query is empty")
    return query, extension, path_prefix, regex


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ExternalChangeError): return "EXTERNAL_CHANGE"
    if isinstance(exc, BlockingIOError): return "LOCKED"
    if isinstance(exc, FileNotFoundError): return "FILE_NOT_FOUND"
    if isinstance(exc, PermissionError): return "PATH_OUTSIDE_WORKSPACE"
    if isinstance(exc, LookupError): return "TEXT_NOT_FOUND"
    if isinstance(exc, (ValueError, IndexError, re.error)): return "INVALID_ARGUMENT"
    if isinstance(exc, NotImplementedError): return "ADAPTER_UNAVAILABLE"
    if isinstance(exc, subprocess.TimeoutExpired): return "PROCESS_TIMEOUT"
    return "COMMAND_FAILED"


def _parse_tokens(value: str) -> list[str]:
    lexer = shlex.shlex(value, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    return list(lexer)


def _scaffold_path(command: str, name: str) -> str:
    if command == "create-test": return f"tests/test_{name.casefold()}.py"
    return f"{name}.py"


def _scaffold_content(command: str, name: str, suffix: str) -> str:
    if command == "create-test":
        if suffix == ".cs": return f"using Xunit;\n\npublic class {name}Tests\n{{\n    [Fact]\n    public void Placeholder()\n    {{\n        Assert.True(true);\n    }}\n}}\n"
        if suffix in {".js", ".ts"}: return f"import {{ describe, it }} from 'node:test';\n\ndescribe('{name}', () => {{\n  it('works', () => {{}});\n}});\n"
        return f"def test_{name.casefold()}():\n    assert True\n"
    if suffix == ".cs":
        if command == "create-interface": return f"public interface {name}\n{{\n}}\n"
        return f"public class {name}\n{{\n}}\n"
    if suffix in {".js", ".ts"}:
        keyword = "interface" if command == "create-interface" else "class"
        return f"export {keyword} {name} {{}}\n"
    if command == "create-interface": return f"from typing import Protocol\n\n\nclass {name}(Protocol):\n    ...\n"
    return f"class {name}:\n    pass\n"


import subprocess
