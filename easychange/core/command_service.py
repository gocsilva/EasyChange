from __future__ import annotations
from .runtime_service import ProjectRuntimeService
from .database_service import DatabaseService
from concurrent.futures import ThreadPoolExecutor
from .safe_edit import SafeEditError, plan_file_mutations, text_hash
from .result_store import DurableResultStore

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
from .maintenance_service import MaintenanceService
from .process_service import ProcessService
from .operation_registry import (
    STRUCTURED_CONTINUE_SAFE_TYPES, STRUCTURED_JOB_TYPES, STRUCTURED_MUTATION_TYPES,
    STRUCTURED_RECOVERY_TYPES, TEXT_MUTATION_COMMANDS, structured_capabilities,
)
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
    """Thread-safe deterministic control plane for CLI diagnostics and HID-driven Machine GUI."""
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.state_store = MachineSession(workspace.root_path / ".easychange" / "session.json", str(workspace.root_path))
        self.session_id = self.state_store.data["session_id"]
        self.instance_id = "A" + secrets.token_hex(3).upper()
        self.files = FileService(workspace, self.state_store.data.get("file_hashes", {}))
        self.git = GitService(workspace)
        self.processes = ProcessService(workspace.root_path)
        self.runtime = ProjectRuntimeService(workspace, self.processes)
        self.database = DatabaseService(workspace)
        self.ids = IdFactory(self.state_store.data.get("id_counts"))
        self.output = self.state_store.data.get("output_mode", "text")
        self.machine = self.state_store.data.get("mode", "human") == "machine"
        self.history: list[str] = list(self.state_store.data.get("history", []))[-50:]
        self.aliases = dict(self.state_store.data.get("aliases", {}))
        self.parser = CommandParser(self.aliases)
        self.indexer = Indexer(workspace.root_path)
        self.symbol_service = SymbolService(workspace, self.indexer)
        # Build the persistent index in the background. Cold searches use git
        # immediately instead of blocking the AI on a full repository scan.
        self.indexer.start_background_refresh()
        self.journal_store = Journal(workspace.root_path / ".easychange" / "journal.jsonl", self.session_id)
        # Compatibility-only recent metadata; full snapshots stay lazy in Journal.
        self.journal: list[Change] = []
        self.locks = LockManager(workspace.root_path / ".easychange" / "locks.json")
        self.transaction_path = workspace.root_path / ".easychange" / "active_transaction.json"
        self.transaction_log_path = workspace.root_path / ".easychange" / "active_transaction.jsonl"
        self.transaction: list[Change] | None = None
        self.transaction_id: str | None = None
        self._transaction_persisted_count = 0
        self._transaction_storage_v2 = False
        self._transaction_dirty_paths: set[str] = set()
        self._load_transaction()
        if self.transaction:
            self._transaction_dirty_paths.update(change.path for change in self.transaction)
        self.results: dict[str, dict] = dict(self.state_store.data.get("results", {}))
        self.file_refs: dict[str, str] = dict(self.state_store.data.get("file_refs", {}))
        if len(self.file_refs) > 1024:
            self.file_refs = dict(list(self.file_refs.items())[-1024:])
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
        # Full EC1 payloads live only in a short in-memory replay cache.
        # Persisted session state keeps hashes/summaries so read-many/study
        # cannot make session.json grow by megabytes per command.
        self._ec_result_cache: dict[str, Result] = {}
        self._ec_result_cache_sizes: dict[str, int] = {}
        self._ec_result_cache_max_items = 8
        self._ec_result_cache_max_bytes = 8 * 1024 * 1024
        self.result_store = DurableResultStore(workspace.root_path / ".easychange" / "results")
        self.maintenance = MaintenanceService(workspace.root_path)
        self._maintenance_interval_seconds = 300.0
        self._maintenance_last = 0.0
        self._maintenance_report: dict = {}
        self._run_maintenance(force=True)

    def _run_maintenance(self, *, force: bool = False, dry_run: bool = False, scan_workspace_temps: bool = False) -> dict:
        now = time.monotonic()
        if (
            not force
            and self._maintenance_report
            and now - self._maintenance_last < self._maintenance_interval_seconds
        ):
            return dict(self._maintenance_report)
        process_state = self.processes.maintenance(keep_finished=8)
        report = self.maintenance.automatic_cleanup(
            referenced_blobs=self.journal_store.referenced_blobs(),
            active_process_ids=self.processes.active_process_ids(),
            dry_run=dry_run,
            scan_workspace_temps=scan_workspace_temps,
        )
        report["process_memory"] = process_state
        report["memory_bounds"] = {
            "history_items": len(self.history),
            "history_limit": 100,
            "journal_entries": self.journal_store.count,
            "journal_recent_metadata": len(self.journal),
            "journal_recent_metadata_limit": 64,
            "ec_result_cache_items": len(self._ec_result_cache),
            "ec_result_cache_bytes": sum(self._ec_result_cache_sizes.values()),
            "ec_result_cache_item_limit": self._ec_result_cache_max_items,
            "ec_result_cache_byte_limit": self._ec_result_cache_max_bytes,
            "navigation_results": len(self.results),
            "navigation_results_limit": 512,
        }
        self._maintenance_last = now
        self._maintenance_report = report
        return dict(report)

    def maintenance_report(self) -> dict:
        return {
            "last": dict(self._maintenance_report),
            "storage": self.maintenance.stats(),
            "memory_bounds": {
                "history_items": len(self.history),
                "history_limit": 100,
                "journal_entries": self.journal_store.count,
                "journal_recent_metadata": len(self.journal),
                "journal_recent_metadata_limit": 64,
                "ec_result_cache_items": len(self._ec_result_cache),
                "ec_result_cache_bytes": sum(self._ec_result_cache_sizes.values()),
                "ec_result_cache_item_limit": self._ec_result_cache_max_items,
                "ec_result_cache_byte_limit": self._ec_result_cache_max_bytes,
                "navigation_results": len(self.results),
                "navigation_results_limit": 512,
            },
        }

    def close(self) -> None:
        if self.watcher:
            self.watcher.stop()
        self.processes.stop_all()
        self.indexer.close()
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
        mutation_types = STRUCTURED_MUTATION_TYPES
        if structured is not None:
            structured_op = str(structured.get("op") or "").casefold()
            structured_items = structured.get("operations") if structured_op == "batch" else [structured]
            is_mutation = any(
                isinstance(item, dict) and str(item.get("type") or "").casefold() in mutation_types
                for item in (structured_items if isinstance(structured_items, list) else [])
            )
        else:
            first = command.strip().split(maxsplit=1)[0].lstrip(":").casefold() if command.strip() else ""
            is_mutation = first in TEXT_MUTATION_COMMANDS

        # Durable store is authoritative for replay. A lost HDMI frame or MCP
        # restart therefore never requires a mutating command to execute twice.
        durable = self.result_store.get(sequence)
        if durable is not None:
            if str(durable.get("command_hash") or "") != digest:
                return Result(
                    False, "ec", error="Sequence reused with different command",
                    code="SEQUENCE_CONFLICT", sequence=sequence,
                )
            if durable.get("state") == "DONE" and isinstance(durable.get("result"), dict):
                replay = Result(**durable["result"])
                replay.sequence = sequence
                replay.data = {
                    **(replay.data or {}),
                    "duplicate": True,
                    "replay": {
                        "state": "DONE",
                        "result_hash": durable.get("result_hash"),
                        "finished_at": durable.get("finished_at"),
                    },
                }
                receipt = durable.get("mutation_receipt")
                if isinstance(receipt, dict):
                    replay.data["mutation_receipt"] = receipt
                return replay
            return Result(
                False,
                "ec",
                error="Sequence is already running; query/replay the same sequence without changing the command",
                code="RUNNING",
                data={
                    "state": durable.get("state") or "RUNNING",
                    "mutation": bool(durable.get("mutation")),
                    "started_at": durable.get("started_at"),
                },
                sequence=sequence,
            )

        # Preserve compatibility with the compact legacy session journal while
        # migrating successful replays to the durable store.
        with self._guard:
            journal = dict(self.state_store.data.get("ec_results", {}))
            previous = journal.get(sequence)
            if previous:
                if previous.get("sha256") != digest:
                    return Result(False, "ec", error="Sequence reused with different command",
                                  code="SEQUENCE_CONFLICT", sequence=sequence)
                cached = self._ec_result_cache.get(sequence)
                if previous.get("state") == "DONE" and cached is not None:
                    replay = Result(**cached.to_dict())
                    replay.sequence = sequence
                    replay.data = {**(replay.data or {}), "duplicate": True}
                    receipt = replay.data.get("mutation_receipt") if isinstance(replay.data, dict) else None
                    self.result_store.mark_running(sequence, digest, command, mutation=is_mutation)
                    self.result_store.complete(
                        sequence, digest, command, replay.to_dict(),
                        mutation_receipt=receipt if isinstance(receipt, dict) else None,
                    )
                    return replay
                legacy = previous.get("result")
                if previous.get("state") == "DONE" and isinstance(legacy, dict):
                    replay = Result(**legacy)
                    replay.sequence = sequence
                    replay.data = {**(replay.data or {}), "duplicate": True}
                    receipt = replay.data.get("mutation_receipt") if isinstance(replay.data, dict) else None
                    self.result_store.mark_running(sequence, digest, command, mutation=is_mutation)
                    self.result_store.complete(
                        sequence, digest, command, replay.to_dict(),
                        mutation_receipt=receipt if isinstance(receipt, dict) else None,
                    )
                    return replay
                if previous.get("state") != "DONE":
                    self.result_store.mark_running(sequence, digest, command, mutation=is_mutation)
                    return Result(
                        False, "ec",
                        error="Execution is already marked RUNNING; mutation will not be replayed blindly",
                        code="RUNNING",
                        data={"state": previous.get("state", "RUNNING"), "mutation": is_mutation},
                        sequence=sequence,
                    )

            journal[sequence] = {
                "sha256": digest,
                "state": "RUNNING",
                "command": command[:256],
                "mutation": is_mutation,
                "started_at": time.time(),
            }
            self.state_store.data["ec_results"] = dict(list(journal.items())[-128:])
            self.result_store.mark_running(sequence, digest, command, mutation=is_mutation)
            if is_mutation:
                self.result_store.update_mutation_state(
                    sequence,
                    "NOT_STARTED",
                    mutation_receipt={
                        "mutation_id": f"M-{sequence}",
                        "sequence": sequence,
                        "state": "NOT_STARTED",
                        "lifecycle_state": "NOT_STARTED",
                        "operations_requested": None,
                        "operations_applied": 0,
                        "matched_occurrences": 0,
                        "validation_result": None,
                        "journal_ids": [],
                        "files": [],
                        "verified": False,
                    },
                )
            self._persist_state()

        try:
            result = self._execute_structured(structured, persist=False, sequence=sequence) if structured is not None else self.execute(command)
        except Exception as exc:
            # A catastrophic exception during a mutating transaction must be
            # converted into an explicit rollback receipt instead of UNKNOWN.
            receipt = None
            if is_mutation and self.transaction is not None:
                try:
                    rollback = self.execute_tokens("rollback", [], raw=":rollback", persist=False)
                    receipt = {
                        "mutation_id": self.transaction_id or f"M-{sequence}",
                        "sequence": sequence,
                        "state": "ROLLED_BACK" if rollback.ok else "ROLLBACK_UNKNOWN",
                        "lifecycle_state": "ROLLED_BACK" if rollback.ok else "FAILED",
                        "verified": bool(rollback.ok),
                    }
                except Exception as rollback_exc:
                    receipt = {
                        "mutation_id": self.transaction_id or f"M-{sequence}",
                        "sequence": sequence,
                        "state": "ROLLBACK_UNKNOWN",
                        "lifecycle_state": "FAILED",
                        "verified": False,
                        "rollback_error": type(rollback_exc).__name__,
                    }
            result = Result(
                False,
                "ec",
                error=str(exc),
                code="MUTATION_ROLLED_BACK" if receipt and receipt.get("state") == "ROLLED_BACK" else type(exc).__name__,
                data={
                    "exception": type(exc).__name__,
                    **({"mutation_receipt": receipt} if receipt else {}),
                },
                sequence=sequence,
            )

        result.sequence = sequence
        if codec:
            result.data = {**(result.data or {}), "transport_codec": codec}

        receipt = result.data.get("mutation_receipt") if isinstance(result.data, dict) else None
        if is_mutation and not isinstance(receipt, dict):
            current = self.result_store.status(sequence) or {}
            prior = current.get("mutation_receipt") if isinstance(current.get("mutation_receipt"), dict) else {}
            receipt = {
                **prior,
                "mutation_id": prior.get("mutation_id") or f"M-{sequence}",
                "sequence": sequence,
                "state": "COMMITTED" if result.ok else "FAILED",
                "lifecycle_state": "APPLIED" if result.ok else "FAILED",
                "operations_applied": prior.get("operations_applied", 1 if result.ok else 0),
                "validation_result": prior.get("validation_result") or {
                    "ok": bool(result.ok),
                    "stage": "legacy_text_mutation",
                },
                "verified": bool(result.ok),
                **({
                    "failure": {
                        "code": result.code,
                        "error": (result.error or "")[:512] or None,
                    }
                } if not result.ok else {}),
            }
            result.data = {**(result.data or {}), "mutation_receipt": receipt}
            self.result_store.update_mutation_state(sequence, str(receipt.get("lifecycle_state") or "FAILED"), mutation_receipt=receipt)
        stored = self.result_store.complete(
            sequence,
            digest,
            command,
            result.to_dict(),
            mutation_receipt=receipt if isinstance(receipt, dict) else None,
        )
        serialized = json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        with self._guard:
            journal = dict(self.state_store.data.get("ec_results", {}))
            journal[sequence] = {
                "sha256": digest,
                "state": "DONE",
                "command": command[:256],
                "mutation": is_mutation,
                "ok": result.ok,
                "command_name": result.command,
                "code": result.code,
                "error": (result.error or "")[:512] or None,
                "duration_ms": result.duration_ms,
                "result_hash": stored.get("result_hash") or hashlib.sha256(serialized).hexdigest(),
                "finished_at": stored.get("finished_at"),
            }
            self.state_store.data["ec_results"] = dict(list(journal.items())[-128:])
            self._remember_ec_result(sequence, result, len(serialized))
            self._persist_state(result)
        return result

    def _remember_ec_result(self, sequence: str, result: Result, serialized_bytes: int) -> None:
        """Keep a small replay hot cache; durable result_store stays authoritative."""
        size = max(0, int(serialized_bytes))
        if size > self._ec_result_cache_max_bytes:
            self._ec_result_cache.pop(sequence, None)
            self._ec_result_cache_sizes.pop(sequence, None)
            return
        self._ec_result_cache.pop(sequence, None)
        self._ec_result_cache_sizes.pop(sequence, None)
        self._ec_result_cache[sequence] = result
        self._ec_result_cache_sizes[sequence] = size
        while (
            len(self._ec_result_cache) > self._ec_result_cache_max_items
            or sum(self._ec_result_cache_sizes.values()) > self._ec_result_cache_max_bytes
        ):
            oldest = next(iter(self._ec_result_cache), None)
            if oldest is None:
                break
            self._ec_result_cache.pop(oldest, None)
            self._ec_result_cache_sizes.pop(oldest, None)

    def _remember_journal_metadata(self, change: Change) -> None:
        self.journal.append(Change(
            path=change.path,
            before=None,
            after=None,
            change_id=change.change_id,
            command=change.command,
        ))
        if len(self.journal) > 64:
            del self.journal[:-64]

    def _execute_planned_mutation_batch(self, operations: list[dict], operation: str,
                                        *, persist: bool, started: float, sequence: str | None = None) -> Result:
        """Plan all file edits against original snapshots, then write each file once."""
        mutation_types = STRUCTURED_MUTATION_TYPES
        normalized: list[dict] = []
        originals: dict[str, str | None] = {}
        actual_hashes: dict[str, str | None] = {}

        try:
            for raw in operations:
                if not isinstance(raw, dict):
                    raise SafeEditError("INVALID_STRUCTURED_PAYLOAD", "OPERATION_OBJECT_REQUIRED")
                item = dict(raw)
                kind = str(item.get("type") or "").casefold()
                if kind == "validate":
                    normalized.append(item)
                    continue
                if kind not in mutation_types:
                    raise SafeEditError("OPERATION_NOT_ALLOWED", f"Unsupported planned mutation: {kind}")
                path = item.get("path")
                if not isinstance(path, str) or not path.strip():
                    raise SafeEditError("PATH_REQUIRED", "Mutation path is required")
                resolved = self.workspace.resolve(path.strip())
                relative = resolved.relative_to(self.workspace.root_path).as_posix()
                item["path"] = relative
                self.locks.check_write(relative, self.instance_id, self.session_id)

                if relative not in originals:
                    if resolved.exists():
                        if not resolved.is_file():
                            raise SafeEditError("NOT_A_FILE", f"Not a file: {relative}")
                        data = resolved.read_bytes()
                        if b"\0" in data:
                            raise SafeEditError("BINARY_FILE", f"Binary file cannot be patched: {relative}")
                        originals[relative] = data.decode("utf-8-sig")
                        actual_hashes[relative] = hashlib.sha256(data).hexdigest()
                        self.files.remember(relative, replace=True)
                    else:
                        originals[relative] = None
                        actual_hashes[relative] = None

                expected_hash = item.get("expected_hash")
                if expected_hash is not None:
                    wanted = str(expected_hash).strip().casefold()
                    actual = str(actual_hashes.get(relative) or "").casefold()
                    if not wanted or not actual.startswith(wanted):
                        raise SafeEditError(
                            "STALE_FILE",
                            f"File changed since study: {relative}",
                            path=relative,
                            expected_hash=wanted,
                            actual_hash=actual,
                        )
                normalized.append(item)

            mutation_ops = [item for item in normalized if str(item.get("type") or "").casefold() in mutation_types]
            plans = plan_file_mutations(originals, mutation_ops)
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "INVALID_STRUCTURED_PAYLOAD")
            details = dict(getattr(exc, "details", {}) or {})
            failure = Result(False, "structured-operation", error=str(exc), code=code, data=details)
            result = Result(
                False, "structured", error=str(exc), code=code,
                data={"schema": "easychange.structured/3", "operation": operation or "operation",
                      "results": [failure.to_dict()], "count": 1,
                      "transaction": {"state": "NOT_STARTED"}, **details},
                command_id=self.ids.next("C"),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            if persist:
                self._persist_state(result)
            return result

        opened = self.execute_tokens("begin", [], raw=":begin", persist=False)
        if not opened.ok:
            return opened
        transaction_id = self.transaction_id
        operation_hash = hashlib.sha256(
            json.dumps(mutation_ops, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        validation_requested = any(
            str(item.get("type") or "").casefold() == "validate" for item in normalized
        )
        matched_occurrences = sum(
            len(plan.edits) if plan.edits else 1 for plan in plans.values()
        )
        base_receipt = {
            "mutation_id": transaction_id,
            "sequence": sequence,
            "state": "STARTED",
            "lifecycle_state": "STARTED",
            "operation_hash": operation_hash,
            "operations_requested": len(mutation_ops),
            "operations_applied": 0,
            "matched_occurrences": matched_occurrences,
            "validation_requested": validation_requested,
            "validation_result": None,
            "journal_ids": [],
            "files": [
                {"path": path, "before_hash": plan.before_hash, "after_hash": plan.after_hash}
                for path, plan in plans.items()
            ],
            "verified": False,
        }

        def persist_receipt(receipt: dict) -> dict:
            if sequence:
                lifecycle = str(receipt.get("lifecycle_state") or receipt.get("state") or "FAILED").upper()
                if lifecycle == "ROLLBACK_UNKNOWN":
                    lifecycle = "FAILED"
                self.result_store.update_mutation_state(
                    sequence,
                    lifecycle,
                    mutation_receipt=receipt,
                )
            return receipt

        persist_receipt(base_receipt)
        write_outcomes: dict[str, dict] = {}
        results: list[dict] = []
        validation_failed = False
        validation_result: dict | None = None
        rollback_data: dict = {"state": "NOT_REQUIRED"}

        try:
            self.last_command = "patch-set"
            # Materialize every final file image exactly once. Coordinates from
            # all operations were resolved against the original snapshots.
            for path, plan in plans.items():
                write_outcomes[path] = self._write(path, plan.after)

            applied_files = [
                {
                    "path": path,
                    "before_hash": plan.before_hash,
                    "after_hash": plan.after_hash,
                    "bytes_written": int(write_outcomes[path].get("bytes_written") or len(plan.after.encode("utf-8"))),
                    "verified": bool(write_outcomes[path].get("verified")),
                    "diff": str(write_outcomes[path].get("diff") or "")[:12000],
                    "changed_ranges": [
                        {
                            "start": edit.start,
                            "end": edit.end,
                            "operation_index": edit.operation_index,
                            "operation_type": edit.operation_type,
                        }
                        for edit in plan.edits
                    ],
                }
                for path, plan in plans.items()
            ]
            persist_receipt({
                **base_receipt,
                "state": "APPLIED",
                "lifecycle_state": "APPLIED",
                "operations_applied": len(mutation_ops),
                "files": applied_files,
                "verified": all(bool(item["verified"]) for item in applied_files),
            })

            for item in normalized:
                kind = str(item.get("type") or "").casefold()
                if kind == "validate":
                    args = ["--test"] if item.get("test") else []
                    sub = self.execute_tokens("validate", args, raw="EC1 validate", persist=False)
                    results.append(sub.to_dict())
                    validation_result = {
                        "ok": bool(sub.ok),
                        "code": sub.code,
                        "error": sub.error,
                        "duration_ms": sub.duration_ms,
                    }
                    if not sub.ok:
                        validation_failed = True
                        break
                    persist_receipt({
                        **base_receipt,
                        "state": "VALIDATED",
                        "lifecycle_state": "VALIDATED",
                        "operations_applied": len(mutation_ops),
                        "files": applied_files,
                        "validation_result": validation_result,
                        "verified": all(bool(item["verified"]) for item in applied_files),
                    })
                    continue

                path = str(item.get("path") or "")
                plan = plans[path]
                outcome = write_outcomes[path]
                results.append(Result(
                    True,
                    kind,
                    data={
                        "path": path,
                        "planned_against": "ORIGINAL_SNAPSHOT",
                        "before_hash": plan.before_hash,
                        "after_hash": plan.after_hash,
                        "verified": bool(outcome.get("verified")),
                    },
                ).to_dict())

            if validation_failed:
                rollback = self.execute_tokens("rollback", [], raw=":rollback", persist=False)
                rollback_data = rollback.data if rollback.ok else {"state": "UNKNOWN", "error": rollback.code}
                receipt = persist_receipt({
                    **base_receipt,
                    "state": "ROLLED_BACK" if rollback.ok else "ROLLBACK_UNKNOWN",
                    "lifecycle_state": "ROLLED_BACK" if rollback.ok else "FAILED",
                    "operations_applied": len(mutation_ops),
                    "files": applied_files,
                    "validation_result": validation_result,
                    "verified": bool(rollback.ok),
                })
                result = Result(
                    False, "structured",
                    data={
                        "schema": "easychange.structured/3",
                        "operation": operation or "operation",
                        "results": results,
                        "count": len(results),
                        "transaction": rollback_data,
                        "mutation_receipt": receipt,
                    },
                    error="Structured batch validation failed and was rolled back",
                    code="STRUCTURED_BATCH_FAILED",
                    command_id=self.ids.next("C"),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                if persist:
                    self._persist_state(result)
                return result

            commit = self.execute_tokens("commit", [], raw=":commit", persist=False)
            if not commit.ok:
                raise RuntimeError(f"TRANSACTION_COMMIT_FAILED:{commit.code or commit.error}")
            receipt_files = applied_files
            receipt = persist_receipt({
                **base_receipt,
                "state": "COMMITTED",
                "lifecycle_state": "VALIDATED" if validation_requested else "APPLIED",
                "operations_applied": len(mutation_ops),
                "files": receipt_files,
                "changed_files": len(receipt_files),
                "journal_ids": list(commit.data.get("journal_ids") or []),
                "validation_result": validation_result or {
                    "ok": True,
                    "stage": "structural_precheck_and_verified_write",
                },
                "verified": all(bool(item["verified"]) for item in receipt_files),
            })
            result = Result(
                True, "structured",
                data={
                    "schema": "easychange.structured/3",
                    "operation": operation or "operation",
                    "results": results,
                    "primary": results[0] if len(results) == 1 else None,
                    "count": len(results),
                    "transaction": {**commit.data, "state": "COMMITTED"},
                    "mutation_receipt": receipt,
                },
                command_id=self.ids.next("C"),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            if persist:
                self._persist_state(result)
            return result
        except Exception as exc:
            if self.transaction is not None:
                try:
                    rollback = self.execute_tokens("rollback", [], raw=":rollback", persist=False)
                    rollback_data = rollback.data if rollback.ok else {"state": "UNKNOWN", "error": rollback.code}
                except Exception as rollback_exc:
                    rollback_data = {"state": "UNKNOWN", "error": type(rollback_exc).__name__}
            rollback_known = rollback_data.get("state") != "UNKNOWN"
            receipt = persist_receipt({
                **base_receipt,
                "state": "ROLLED_BACK" if rollback_known else "ROLLBACK_UNKNOWN",
                "lifecycle_state": "ROLLED_BACK" if rollback_known else "FAILED",
                "operations_applied": len(write_outcomes),
                "files": [
                    {
                        "path": path,
                        "before_hash": plan.before_hash,
                        "after_hash": plan.after_hash,
                        "verified": bool((write_outcomes.get(path) or {}).get("verified")),
                    }
                    for path, plan in plans.items()
                ],
                "validation_result": validation_result,
                "verified": rollback_known,
                "failure": {"type": type(exc).__name__, "error": str(exc)[:512]},
            })
            result = Result(
                False, "structured",
                data={
                    "schema": "easychange.structured/3",
                    "operation": operation or "operation",
                    "results": results,
                    "transaction": rollback_data,
                    "mutation_receipt": receipt,
                },
                error=str(exc),
                code=str(getattr(exc, "code", "") or type(exc).__name__),
                command_id=self.ids.next("C"),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            if persist:
                self._persist_state(result)
            return result
    def _read_regions_data(self, regions: list[dict], *, max_bytes: int = 2097152,
                           merge_overlaps: bool = True) -> dict:
        """Bulk-read arbitrary source ranges with overlap coalescing and a hard response budget."""
        if not isinstance(regions, list) or not 1 <= len(regions) <= 64:
            raise ValueError("read_regions requires 1 to 64 regions")
        normalized: list[dict] = []
        for item in regions:
            if not isinstance(item, dict):
                raise ValueError("read_regions entries must be objects")
            path = item.get("path")
            start = item.get("start", 1)
            count = item.get("count", 40)
            if not isinstance(path, str) or not path.strip():
                raise ValueError("read_regions path is required")
            if isinstance(start, bool) or not isinstance(start, int):
                raise ValueError("read_regions start must be an integer")
            if isinstance(count, bool) or not isinstance(count, int):
                raise ValueError("read_regions count must be an integer")
            normalized.append({
                "path": self._resolve_ref(path.strip()),
                "start": max(1, int(start)),
                "count": max(1, min(2400, int(count))),
            })

        requested_count = len(normalized)
        if merge_overlaps:
            grouped: dict[str, list[tuple[int, int]]] = {}
            order: list[str] = []
            for item in normalized:
                path = item["path"]
                if path not in grouped:
                    grouped[path] = []
                    order.append(path)
                start = int(item["start"])
                grouped[path].append((start, start + int(item["count"]) - 1))
            merged: list[dict] = []
            for path in order:
                ranges = sorted(grouped[path])
                current_start = current_end = None
                for start, end in ranges:
                    if current_start is None:
                        current_start, current_end = start, end
                    elif start <= current_end + 2:
                        current_end = max(current_end, end)
                    else:
                        merged.append({"path": path, "start": current_start,
                                       "count": current_end - current_start + 1})
                        current_start, current_end = start, end
                if current_start is not None:
                    merged.append({"path": path, "start": current_start,
                                   "count": current_end - current_start + 1})
            normalized = merged[:64]

        def safe_read(spec: dict) -> tuple[dict, dict | None, dict | None]:
            try:
                info = self.files.read(spec["path"], spec["start"], spec["count"])
                return spec, info, None
            except Exception as exc:
                return spec, None, {
                    "path": spec["path"],
                    "start": spec["start"],
                    "count": spec["count"],
                    "error": str(exc)[:512],
                    "code": str(getattr(exc, "code", "") or type(exc).__name__),
                }

        workers = min(12, len(normalized))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="easychange-regions") as pool:
            loaded = list(pool.map(safe_read, normalized))

        max_bytes = max(32768, min(8388608, int(max_bytes)))
        output: list[dict] = []
        skipped: list[dict] = []
        errors: list[dict] = []
        total_bytes = 0
        for position, (spec, info, error) in enumerate(loaded):
            if error is not None:
                errors.append(error)
                continue
            assert info is not None
            value = {
                "path": info.get("path") or spec["path"],
                "start": info.get("start", spec["start"]),
                "requested_count": spec["count"],
                "returned_count": len(info.get("lines") or []),
                "total_lines": info.get("total_lines"),
                "hash": info.get("hash"),
                "lines": list(info.get("lines") or []),
                "has_more": bool(info.get("has_more")),
                "next_start": info.get("next_start"),
            }
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if total_bytes + len(encoded) > max_bytes:
                if not output:
                    lines = list(value["lines"])
                    while len(encoded) > max_bytes and len(lines) > 1:
                        lines = lines[:max(1, len(lines) // 2)]
                        value["lines"] = lines
                        value["returned_count"] = len(lines)
                        value["budget_truncated"] = True
                        value["has_more"] = True
                        value["next_start"] = int(value["start"]) + len(lines)
                        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    output.append(value)
                    total_bytes += len(encoded)
                    skipped.extend(item[0] for item in loaded[position + 1:] if item[2] is None)
                else:
                    skipped.extend(item[0] for item in loaded[position:] if item[2] is None)
                break
            output.append(value)
            total_bytes += len(encoded)

        budget_truncated = bool(skipped) or any(bool(item.get("budget_truncated")) for item in output)
        return {
            "regions": output,
            "count": len(output),
            "requested": requested_count,
            "coalesced": len(normalized),
            "raw_bytes": total_bytes,
            "byte_budget": max_bytes,
            "truncated": budget_truncated,
            "skipped": skipped,
            "errors": errors,
            "failed": len(errors),
            "partial": bool(errors) or budget_truncated,
            "has_more": bool(skipped),
            "remaining": len(skipped),
            "parallel_workers": workers,
            "merge_overlaps": bool(merge_overlaps),
        }

    def _execute_structured(self, payload: dict, *, persist: bool = True, sequence: str | None = None) -> Result:
        """Execute bounded HID-delivered operations; read-only batches avoid transactions."""
        started = time.monotonic()
        operation = str(payload.get("op") or "").casefold()
        operations = payload.get("operations") if operation == "batch" else [payload]
        if not isinstance(operations, list) or not 1 <= len(operations) <= 64:
            return Result(False, "structured", error="OPERATION_COUNT_LIMIT", code="INVALID_STRUCTURED_PAYLOAD")

        # Small durable-result queries intentionally bypass project/index work.
        # They are the recovery API used when HDMI lost the original frame.
        if len(operations) == 1 and isinstance(operations[0], dict):
            query_item = operations[0]
            query_kind = str(query_item.get("type") or "").casefold()
            if query_kind in STRUCTURED_RECOVERY_TYPES:
                target_sequence = str(query_item.get("sequence") or query_item.get("target_sequence") or "").upper()
                if not re.fullmatch(r"Q[A-Z0-9_-]{4,40}", target_sequence):
                    return Result(False, query_kind, error="VALID_TARGET_SEQUENCE_REQUIRED", code="INVALID_SEQUENCE")
                if query_kind in {"result_status", "mutation_status"}:
                    data = self.result_store.status(target_sequence)
                elif query_kind == "result_header":
                    data = self.result_store.header(
                        target_sequence,
                        chunk_bytes=int(query_item.get("chunk_bytes") or 640),
                    )
                elif query_kind == "result_get":
                    data = self.result_store.result(target_sequence)
                elif query_kind == "result_meta":
                    data = self.result_store.chunk_meta(target_sequence, chunk_bytes=int(query_item.get("chunk_bytes") or 320))
                elif query_kind == "result_chunk":
                    try:
                        data = self.result_store.chunk(
                            target_sequence,
                            int(query_item.get("chunk_index")),
                            chunk_bytes=int(query_item.get("chunk_bytes") or 320),
                        )
                    except (TypeError, ValueError, IndexError) as exc:
                        return Result(False, query_kind, error=str(exc), code="INVALID_RESULT_CHUNK")
                else:
                    original_result = self.result_store.result(target_sequence)
                    if original_result is None:
                        data = None
                    else:
                        from ..remote.optical_protocol import encode_result_chunks
                        packets = encode_result_chunks(original_result)
                        if query_kind == "optical_meta":
                            first = json.loads(packets[0])
                            data = {
                                "target_sequence": target_sequence,
                                "total_chunks": len(packets),
                                "result_hash": first.get("h"),
                                "crc32": first.get("c"),
                            }
                        else:
                            try:
                                chunk_index = int(query_item.get("chunk_index"))
                                if chunk_index < 0 or chunk_index >= len(packets):
                                    raise IndexError("OPTICAL_CHUNK_OUT_OF_RANGE")
                                data = {
                                    "target_sequence": target_sequence,
                                    "chunk_index": chunk_index,
                                    "packet": json.loads(packets[chunk_index]),
                                }
                            except (TypeError, ValueError, IndexError) as exc:
                                return Result(False, query_kind, error=str(exc), code="INVALID_RESULT_CHUNK")
                if data is None:
                    return Result(False, query_kind, error="RESULT_NOT_FOUND", code="RESULT_NOT_FOUND")
                return Result(True, query_kind, data=data, command_id=self.ids.next("C"),
                              duration_ms=int((time.monotonic() - started) * 1000))

        # Long-running build/test/process work is detached from the visual
        # result frame. A small job receipt returns immediately while terminal
        # output remains durable under .easychange/jobs.
        if len(operations) == 1 and isinstance(operations[0], dict):
            job_item = operations[0]
            job_kind = str(job_item.get("type") or "").casefold()
            if job_kind in STRUCTURED_JOB_TYPES:
                try:
                    job_id = str(job_item.get("job_id") or "")
                    if job_kind == "job_start":
                        argv = job_item.get("argv")
                        if not isinstance(argv, list) or not 1 <= len(argv) <= 32 or not all(
                            isinstance(value, str) and value for value in argv
                        ):
                            return Result(False, job_kind, error="ARGV_REQUIRES_1_TO_32_STRINGS",
                                          code="INVALID_STRUCTURED_PAYLOAD")
                        if not job_id:
                            job_id = self.ids.next("J")
                        data = self.processes.job_start(argv, job_id)
                    elif job_kind == "job_status":
                        if not job_id:
                            return Result(False, job_kind, error="JOB_ID_REQUIRED", code="INVALID_JOB_ID")
                        data = self.processes.job_status(job_id)
                    elif job_kind == "job_result":
                        if not job_id:
                            return Result(False, job_kind, error="JOB_ID_REQUIRED", code="INVALID_JOB_ID")
                        data = self.processes.job_result(job_id)
                    else:
                        if not job_id:
                            return Result(False, job_kind, error="JOB_ID_REQUIRED", code="INVALID_JOB_ID")
                        data = self.processes.job_cancel(job_id)
                    return Result(True, job_kind, data=data, command_id=self.ids.next("C"),
                                  duration_ms=int((time.monotonic() - started) * 1000))
                except Exception as exc:
                    return Result(False, job_kind, error=str(exc),
                                  code=str(getattr(exc, "code", "") or type(exc).__name__),
                                  command_id=self.ids.next("C"),
                                  duration_ms=int((time.monotonic() - started) * 1000))

        mutation_types = STRUCTURED_MUTATION_TYPES
        kinds = [str(item.get("type") or "").casefold() if isinstance(item, dict) else "" for item in operations]
        transactional = any(kind in mutation_types for kind in kinds)
        continue_on_error = bool(payload.get("continue_on_error", False))
        if continue_on_error and (transactional or not all(kind in STRUCTURED_CONTINUE_SAFE_TYPES for kind in kinds)):
            return Result(False, "structured", error="CONTINUE_ON_ERROR_REQUIRES_SAFE_READ_BATCH",
                          code="INVALID_STRUCTURED_PAYLOAD")
        if transactional and self.transaction is not None:
            return Result(False, "structured", error="BATCH_CANNOT_NEST_TRANSACTION", code="TRANSACTION_ACTIVE")

        # Mutating HID batches are planned completely against immutable original
        # snapshots before the first byte is written. This prevents stale line
        # coordinates when an earlier edit changes line counts.
        if transactional and all(kind in mutation_types or kind == "validate" for kind in kinds):
            return self._execute_planned_mutation_batch(operations, operation, persist=persist, started=started, sequence=sequence)

        if transactional:
            # begin persists the active transaction itself; avoid the second
            # generic execute_tokens session save.
            opened = self.execute_tokens("begin", [], raw=":begin", persist=False)
            if not opened.ok:
                return opened

        results = []
        # The transaction log is the durable per-edit record. session.json is
        # UI/navigation state and does not need an extra write after every
        # sub-operation.
        sub_persist = False
        for item in operations:
            item_started = time.monotonic()
            if not isinstance(item, dict):
                result = Result(False, "structured", error="OPERATION_OBJECT_REQUIRED", code="INVALID_STRUCTURED_PAYLOAD")
            else:
                kind = str(item.get("type") or "").casefold()
                guard_error = None
                expected_hash = item.get("expected_hash")
                guard_path = item.get("path")
                if expected_hash is not None and kind in mutation_types:
                    if not isinstance(expected_hash, str) or not expected_hash.strip() or not isinstance(guard_path, str):
                        guard_error = Result(False, kind, error="EXPECTED_HASH_REQUIRES_PATH",
                                             code="INVALID_STRUCTURED_PAYLOAD")
                    elif self.files.exists(guard_path):
                        actual_hash = self.files.hash(guard_path)
                        wanted = expected_hash.strip().casefold()
                        if not actual_hash.casefold().startswith(wanted):
                            guard_error = Result(
                                False, kind, error="File changed since study",
                                code="STALE_FILE",
                                data={"path": guard_path, "expected_hash": wanted,
                                      "actual_hash": actual_hash},
                            )
                    elif kind != "create_file":
                        guard_error = Result(False, kind, error=f"File not found: {guard_path}",
                                             code="FILE_NOT_FOUND")

                if guard_error is not None:
                    result = guard_error
                elif kind in {"write_file", "create_file"}:
                    path, content = item.get("path"), item.get("content")
                    if not isinstance(path, str) or not isinstance(content, str):
                        result = Result(False, kind, error="PATH_AND_CONTENT_REQUIRED", code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        try:
                            if kind == "create_file" and self.workspace.resolve(path).exists():
                                result = Result(False, kind, error=f"File already exists: {path}", code="FILE_EXISTS")
                            else:
                                result = self.execute_tokens("write", [path, content], raw=f"EC1 {kind} {path}", persist=sub_persist)
                        except Exception as exc:
                            result = Result(False, kind, error=str(exc), code=getattr(exc, "code", type(exc).__name__))
                elif kind == "replace_line":
                    path, line, content = item.get("path"), item.get("line"), item.get("content")
                    if not isinstance(path, str) or not isinstance(content, str) or isinstance(line, bool) or not isinstance(line, int):
                        result = Result(False, kind, error="PATH_LINE_AND_CONTENT_REQUIRED", code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        result = self.execute_tokens("replace-line", [path, str(line), content], raw=f"EC1 patch {path}:{line}", persist=sub_persist)
                elif kind == "replace_range":
                    path, start, end, content = item.get("path"), item.get("start"), item.get("end"), item.get("content")
                    if (not isinstance(path, str) or not isinstance(content, str)
                            or isinstance(start, bool) or not isinstance(start, int)
                            or isinstance(end, bool) or not isinstance(end, int)):
                        result = Result(False, kind, error="PATH_START_END_AND_CONTENT_REQUIRED",
                                        code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        result = self.execute_tokens("replace-range",
                            [path, str(start), str(end), content], raw=f"EC1 replace-range {path}:{start}-{end}", persist=sub_persist)
                elif kind == "replace_text":
                    path, old, new = item.get("path"), item.get("old"), item.get("new")
                    if not isinstance(path, str) or not isinstance(old, str) or not isinstance(new, str):
                        result = Result(False, kind, error="PATH_OLD_AND_NEW_REQUIRED",
                                        code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        result = self.execute_tokens("replace", [path, old, new], raw=f"EC1 replace {path}", persist=sub_persist)
                elif kind == "insert":
                    path, line, content = item.get("path"), item.get("line"), item.get("content")
                    if (not isinstance(path, str) or not isinstance(content, str)
                            or isinstance(line, bool) or not isinstance(line, int)):
                        result = Result(False, kind, error="PATH_LINE_AND_CONTENT_REQUIRED",
                                        code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        result = self.execute_tokens("insert", [path, str(line), content], raw=f"EC1 insert {path}:{line}", persist=sub_persist)
                elif kind == "append":
                    path, content = item.get("path"), item.get("content")
                    if not isinstance(path, str) or not isinstance(content, str):
                        result = Result(False, kind, error="PATH_AND_CONTENT_REQUIRED",
                                        code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        result = self.execute_tokens("append", [path, content], raw=f"EC1 append {path}", persist=sub_persist)
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
                        result = self.execute_tokens("read", args, raw=f"EC1 read {path}", persist=sub_persist)
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
                        result = self.execute_tokens(kind, args, raw=f"EC1 {kind} {query}", persist=sub_persist)
                elif kind == "discover_many":
                    terms = item.get("terms")
                    if not isinstance(terms, list) or not 1 <= len(terms) <= 32 or not all(
                        isinstance(term, str) and term.strip() for term in terms
                    ):
                        result = Result(False, kind, error="TERMS_REQUIRES_1_TO_32_STRINGS",
                                        code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        data = self.indexer.discover_many(
                            terms,
                            limit_per_term=max(1, min(50, int(item.get("limit") or 12))),
                            definitions_per_term=max(1, min(20, int(item.get("definitions") or 4))),
                            references_per_term=max(1, min(100, int(item.get("references") or 12))),
                            extension=str(item.get("extension") or "").strip() or None,
                            path_prefix=str(item.get("path_prefix") or "").strip() or None,
                        )
                        radius = max(0, min(30, int(item.get("context") or 0)))
                        if radius > 0:
                            context_files = max(1, min(16, int(item.get("context_files") or 8)))
                            hits_per_file = max(1, min(8, int(item.get("hits_per_file") or 2)))
                            selected_paths = [
                                str(entry.get("file"))
                                for entry in (data.get("impact_files") or [])[:context_files]
                                if isinstance(entry, dict) and entry.get("file")
                            ]
                            regions = []
                            by_term = data.get("by_term") or {}
                            for path in selected_paths:
                                lines = []
                                for bucket in by_term.values():
                                    if not isinstance(bucket, dict):
                                        continue
                                    for ref in list(bucket.get("definitions") or []) + list(bucket.get("references") or []):
                                        if isinstance(ref, dict) and str(ref.get("file") or "") == path:
                                            try:
                                                lines.append(int(ref.get("line")))
                                            except (TypeError, ValueError):
                                                pass
                                chosen = []
                                for line_no in sorted(set(lines)):
                                    if any(abs(line_no - prior) <= radius for prior in chosen):
                                        continue
                                    chosen.append(line_no)
                                    if len(chosen) >= hits_per_file:
                                        break
                                for line_no in chosen:
                                    regions.append({
                                        "path": path,
                                        "start": max(1, line_no - radius),
                                        "count": radius * 2 + 1,
                                    })
                            if regions:
                                data["context_pack"] = self._read_regions_data(
                                    regions,
                                    max_bytes=max(32768, min(
                                        4194304, int(item.get("context_max_bytes") or 1048576)
                                    )),
                                    merge_overlaps=True,
                                )
                            else:
                                data["context_pack"] = {
                                    "regions": [], "count": 0, "requested": 0,
                                    "coalesced": 0, "raw_bytes": 0, "truncated": False,
                                }
                        for bucket in (data.get("by_term") or {}).values():
                            if isinstance(bucket, dict) and isinstance(bucket.get("references"), list):
                                bucket["references"] = self._register_matches(bucket["references"])
                        result = Result(True, kind, data=data)
                elif kind == "read_regions":
                    regions = item.get("regions")
                    if not isinstance(regions, list):
                        result = Result(False, kind, error="REGIONS_REQUIRED",
                                        code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        try:
                            data = self._read_regions_data(
                                regions,
                                max_bytes=int(item.get("max_bytes") or 2097152),
                                merge_overlaps=bool(item.get("merge_overlaps", True)),
                            )
                            result = Result(True, kind, data=data)
                        except (ValueError, TypeError, OSError, PermissionError, FileNotFoundError) as exc:
                            result = Result(False, kind, error=str(exc),
                                            code=str(getattr(exc, "code", "") or "INVALID_READ_REGIONS"))
                elif kind == "read_many":
                    paths = item.get("paths")
                    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                        result = Result(False, kind, error="PATHS_REQUIRED", code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        args = list(paths)
                        if isinstance(item.get("count"), int):
                            args += ["--count", str(item["count"])]
                        if isinstance(item.get("max_bytes"), int):
                            args += ["--max-bytes", str(item["max_bytes"])]
                        result = self.execute_tokens("read-many", args, raw="EC1 read-many", persist=sub_persist)
                elif kind == "runtime_profiles":
                    profiles = self.runtime.profiles()
                    ecosystems: dict[str, int] = {}
                    for profile in profiles:
                        ecosystem = str(profile.get("ecosystem") or "unknown")
                        ecosystems[ecosystem] = ecosystems.get(ecosystem, 0) + 1
                    result = Result(True, kind, data={
                        "profiles": profiles,
                        "count": len(profiles),
                        "runnable": sum(1 for profile in profiles if profile.get("runnable")),
                        "ecosystems": ecosystems,
                    })
                elif kind == "runtime_configure":
                    result = Result(True, kind, data=self.runtime.configure_profile(
                        str(item.get("profile_id") or ""), kind=str(item.get("kind") or "custom"),
                        run_argv=item.get("run_argv"), test_argv=item.get("test_argv"),
                        urls=item.get("urls") if isinstance(item.get("urls"), list) else None,
                    ))
                elif kind == "run_project":
                    profile = str(item.get("profile") or "")
                    process_id = self.ids.next("P")
                    result = Result(True, kind, data=self.runtime.start(process_id, profile))
                elif kind == "test_smart":
                    profile = str(item.get("profile") or "")
                    timeout = max(5, min(3600, int(item.get("timeout") or 600)))
                    data = self.runtime.smart_test(profile, timeout)
                    result = Result(bool(data.get("passed")), kind, data=data,
                                    error=None if data.get("passed") else "Smart test failed",
                                    code=None if data.get("passed") else "TEST_FAILED")
                elif kind == "test_evidence":
                    profile = str(item.get("profile") or "")
                    timeout = max(5, min(3600, int(item.get("timeout") or 600)))
                    data = self.runtime.test_with_evidence(
                        profile, timeout, str(item.get("title") or "test-evidence")
                    )
                    result = Result(bool(data.get("passed")), kind, data=data,
                                    error=None if data.get("passed") else "Test evidence failed",
                                    code=None if data.get("passed") else "TEST_FAILED")
                elif kind == "process_logs":
                    process_id = str(item.get("process_id") or "")
                    result = Result(True, kind, data=self.processes.logs(
                        process_id, limit=max(512, min(100000, int(item.get("limit") or 12000)))
                    ))
                elif kind == "stop_process":
                    process_id = str(item.get("process_id") or "")
                    result = Result(True, kind, data=self.processes.stop(process_id))
                elif kind == "swagger_evidence":
                    profile = str(item.get("profile") or "")
                    url = str(item.get("url") or "")
                    timeout = max(5.0, min(180.0, float(item.get("timeout") or 45.0)))
                    process_id = self.ids.next("P")
                    data = self.runtime.swagger_evidence(
                        process_id, profile, url, timeout,
                        bool(item.get("screenshot", True)), bool(item.get("start_if_needed", True)),
                        bool(item.get("keep_running", False))
                    )
                    result = Result(bool(data.get("ok")), kind, data=data,
                                    error=None if data.get("ok") else "Swagger evidence failed",
                                    code=None if data.get("ok") else "EVIDENCE_FAILED")
                elif kind == "evidence":
                    result = Result(True, kind, data=self.runtime.evidence(
                        str(item.get("title") or "test-evidence"), str(item.get("process_id") or "")
                    ))
                elif kind == "db_connections":
                    result = Result(True, kind, data=self.database.connections())
                elif kind == "db_configure":
                    result = Result(True, kind, data=self.database.configure(
                        str(item.get("name") or ""), str(item.get("provider") or ""),
                        item.get("settings") if isinstance(item.get("settings"), dict) else {},
                    ))
                elif kind == "db_schema":
                    connection = str(item.get("connection") or "")
                    if not connection:
                        result = Result(False, kind, error="CONNECTION_REQUIRED", code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        result = Result(True, kind, data=self.database.schema(
                            connection, max_rows=max(1, min(5000, int(item.get("max_rows") or 500)))
                        ))
                elif kind == "db_query":
                    connection = str(item.get("connection") or "")
                    sql = str(item.get("sql") or "")
                    allow_write = bool(item.get("allow_write", False))
                    if len(operations) > 1 and allow_write:
                        result = Result(False, kind, error="WRITE_DB_QUERY_CANNOT_BE_MIXED_IN_BATCH",
                                        code="NON_ATOMIC_EXTERNAL_WRITE")
                    elif not connection or not sql:
                        result = Result(False, kind, error="CONNECTION_AND_SQL_REQUIRED", code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        data = self.database.query(
                            connection, sql, allow_write=allow_write,
                            max_rows=max(1, min(5000, int(item.get("max_rows") or 200))),
                            timeout=max(1, min(300, int(item.get("timeout") or 30))),
                        )
                        result = Result(True, kind, data=data)
                elif kind == "validate":
                    args = ["--test"] if item.get("test") else []
                    result = self.execute_tokens("validate", args, raw="EC1 validate", persist=sub_persist)
                else:
                    result = Result(False, "structured", error=f"Unsupported operation: {kind}", code="OPERATION_NOT_ALLOWED")
            if not result.command_id:
                result.command_id = self.ids.next("C")
            if not result.duration_ms:
                result.duration_ms = int((time.monotonic() - item_started) * 1000)
            results.append(result.to_dict())
            if not result.ok and not continue_on_error:
                break

        succeeded = sum(1 for item in results if item.get("ok"))
        failed = len(results) - succeeded
        summary = {
            "requested": len(operations), "completed": len(results), "succeeded": succeeded,
            "failed": failed, "skipped": max(0, len(operations) - len(results)),
            "continue_on_error": continue_on_error,
        }
        ok = len(results) == len(operations) and failed == 0
        transaction_data = {"state": "NOT_REQUIRED"}
        if transactional:
            transaction = self.execute_tokens(
                "commit" if ok else "rollback", [],
                raw=":commit" if ok else ":rollback", persist=False
            )
            transaction_data = transaction.data if transaction.ok else {"state": "UNKNOWN", "error": transaction.code}
            ok = bool(ok and transaction.ok)

        result = Result(
            ok,
            "structured",
            data={
                "schema": "easychange.structured/2",
                "operation": operation or "operation",
                "results": results,
                "primary": results[0] if len(results) == 1 else None,
                "count": len(results),
                "summary": summary,
                "transaction": transaction_data,
            },
            error=None if ok else ("Structured batch failed and was rolled back" if transactional else "Structured read batch failed"),
            code=None if ok else "STRUCTURED_BATCH_FAILED",
            command_id=self.ids.next("C"),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if persist:
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

    def _error_result(self, raw: str, exc: Exception, *, persist: bool = True) -> Result:
        if isinstance(exc, ProcessFailure):
            result = exc.result
            result.command_id = self.ids.next("C")
            if persist:
                self._persist_state(result)
            return result
        data = {}
        if isinstance(exc, ExternalChangeError):
            data = {"file": exc.path, "expected_hash": exc.expected, "actual_hash": exc.actual,
                    "options": ["reload", "diff", "force-write"]}
        result = Result(False, raw.split(maxsplit=1)[0].lstrip(":").lower(), error=str(exc),
                        code=_error_code(exc), command_id=self.ids.next("C"), data=data)
        if persist:
            self._persist_state(result)
        return result

    def execute_tokens(self, name: str, args: list[str], *, raw: str | None = None,
                       persist: bool = True) -> Result:
        """Execute already-tokenized input for deterministic transports."""
        started = time.monotonic()
        name = name.lower()
        if name in self.aliases:
            name = self.aliases[name]
        self.history.append(raw if raw is not None else ":" + " ".join([name, *args]))
        if len(self.history) > 100:
            del self.history[:-100]
        self.last_command = name
        try:
            with self._guard:
                data = self._dispatch(name, list(args))
            result = Result(True, name, data=data, command_id=self.ids.next("C"))
        except Exception as exc:
            # _error_result can persist when called directly. Here the caller
            # owns the one final persistence decision, avoiding a double save
            # for every failed execute_tokens call.
            result = self._error_result(name, exc, persist=False)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        self.last_command = name
        self.last_result = result
        if name not in {"health", "maintenance"}:
            try:
                self._run_maintenance()
            except (OSError, ValueError, RuntimeError):
                pass
        if persist:
            self._persist_state(result)
        return result

    @staticmethod
    def _result_summary(result: Result | None) -> dict | None:
        if result is None:
            return None
        data = result.data or {}
        compact_data = {}
        for key in ("path", "file", "count", "state", "workspace", "line", "transaction", "passed"):
            value = data.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                if key in data:
                    compact_data[key] = value
        return {
            "ok": result.ok,
            "command": result.command,
            "code": result.code,
            "error": (result.error or "")[:512] or None,
            "command_id": result.command_id,
            "duration_ms": result.duration_ms,
            "sequence": result.sequence,
            "data": compact_data,
        }

    def _persist_state(self, result: Result | None = None) -> None:
        self.state_store.data.update({"session_id": self.session_id, "workspace": str(self.workspace.root_path),
            "current_file": self.state_store.data.get("current_file"), "output_mode": self.output,
            "transport": self.state_store.data.get("transport", "local"), "mode": "machine" if self.machine else "human",
            "history": self.history[-50:], "id_counts": self.ids.counts(), "aliases": self.aliases,
            "macros": self.macros, "snapshots": self.snapshots,
            "last_command": self.last_command, "last_result": self._result_summary(result),
            "file_refs": dict(list(self.file_refs.items())[-1024:]),
            "results": dict(list(self.results.items())[-512:])})
        self.state_store.data["file_hashes"] = self.files.baselines
        # Defensive migration: old builds may have left full result payloads
        # inside ec_results. Compact them before every save.
        compact_journal = {}
        for sequence, item in dict(self.state_store.data.get("ec_results", {})).items():
            if not isinstance(item, dict):
                continue
            if item.get("state") == "DONE" and isinstance(item.get("result"), dict):
                legacy = item["result"]
                encoded = json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                item = {
                    "sha256": item.get("sha256"),
                    "state": "DONE",
                    "command": item.get("command"),
                    "ok": bool(legacy.get("ok")),
                    "command_name": legacy.get("command"),
                    "code": legacy.get("code"),
                    "error": str(legacy.get("error") or "")[:512] or None,
                    "duration_ms": legacy.get("duration_ms", 0),
                    "result_hash": hashlib.sha256(encoded).hexdigest(),
                }
            compact_journal[sequence] = item
        self.state_store.data["ec_results"] = dict(list(compact_journal.items())[-128:])
        self.state_store.save()

    def _dispatch(self, name: str, args: list[str]) -> dict:
        if name in {
            "files", "tree", "projects", "next", "prev",
            "search", "locate", "study", "discover-many",
            "symbols", "symbol", "definition", "references", "implementations", "outline",
        } and self._transaction_dirty_paths:
            # Preserve read-after-write semantics while allowing mutation-only
            # batches to index each changed file once at commit.
            self._flush_transaction_index()
        if name in {"help", "capabilities"}:
            return {"commands": ["state", "workspace", "pwd", "files", "tree", "next", "prev", "read", "head", "tail", "context", "goto",
                    "search", "find", "index", "symbols", "symbol", "definition", "references", "implementations", "outline", "file-summary",
                    "locate", "study", "discover-many", "read-many", "validate", "edit-result",
                    "runtime-profile", "run-project", "test-smart", "test-evidence",
                    "swagger-evidence", "evidence", "process-logs",
                    "db-connections", "db-schema", "db-query",
                    "new", "mkdir", "write", "append", "insert", "replace", "replace-line", "replace-range", "delete", "rename", "move", "stat", "hash", "exists",
                    "rename-symbol", "create-class", "create-interface", "create-test", "format", "fix-imports", "organize-imports",
                    "journal", "undo", "redo", "begin", "commit", "rollback", "snapshot", "lock", "unlock", "locks", "watch", "batch", "macro", "alias", "complete", "history", "repeat", "clear",
                    "status", "health", "maintenance", "diff", "branch", "log", "build", "test", "run", "processes", "stop", "errors", "next-error", "previous-error", "remote", "remote-guide", "remote-profile", "prepare-hid", "set", "machine", "human", "quit"],
                    "capabilities": {"workspace": True, "git": "GIT" in self.workspace.adapters,
                    "build_test": self.workspace.adapters, "index": True, "symbols": True,
                    "ai_machine": {"optical_protocol": "EC2", "qr_slots": 16, "chunked_results": True,
                                   "optical_burst": True, "composite_study": True, "read_many": True,
                                   "mass_discovery": True, "mass_discovery_max_terms": 32,
                                   "cold_discovery": "single-git-grep-multi-pattern",
                                   "mass_read_regions": True, "read_regions_max": 64,
                                   "discovery_context_pack": True,
                                   "read_many_max_files": 64, "read_many_byte_budget": True,
                                   "structured_max_operations": 64, "expected_hash_guards": True,
                                   "structured_operations": structured_capabilities(),
                                    "automatic_maintenance": True, "maintenance_interval_seconds": 300,
                                   "validate": True, "cold_search": "git-grep", "warm_search": "sqlite-fts5"},
                    "runtime": {"profiles": True, "smart_test": True, "background_run": True,
                                "swagger_evidence": True, "local_screenshots": True},
                    "database": {"read_only_default": True, "sqlite": True, "sqlserver_cli": True,
                                 "postgres_cli": True, "mysql_cli": True},
                    "remote_control": {"input": "ESP32_HID", "output": "HDMI",
                                       "mcp_on_remote_pc": False, "api_on_remote_pc": False}}}
        if name == "maintenance":
            dry_run = "--dry-run" in args
            scan_workspace_temps = "--workspace-temps" in args
            return self._run_maintenance(
                force=True,
                dry_run=dry_run,
                scan_workspace_temps=scan_workspace_temps,
            )
        if name == "health":
            index = self.indexer.status()
            processes = self.processes.list()
            active_processes = sum(1 for item in processes if str(item.get("state") or "").upper() == "RUNNING")
            transaction_state = "ACTIVE" if self.transaction is not None else "NONE"
            degraded_reasons = []
            if transaction_state == "ACTIVE":
                degraded_reasons.append("TRANSACTION_ACTIVE")
            if str(index.get("state") or index.get("status") or "").upper() in {"FAILED", "ERROR"}:
                degraded_reasons.append("INDEX_ERROR")
            return {
                "healthy": not degraded_reasons,
                "state": "READY" if not degraded_reasons else "DEGRADED",
                "workspace": str(self.workspace.root_path),
                "workspace_type": self.workspace.kind,
                "index": index,
                "transaction": transaction_state,
                "machine_mode": self.machine,
                "transport": self.state_store.data.get("transport", "local"),
                "process_count": len(processes),
                "active_processes": active_processes,
                "durable_results": True,
                "structured_operations": structured_capabilities(),
                "maintenance": self.maintenance_report(),
                "degraded_reasons": degraded_reasons,
            }
        if name == "state":
            compact = "--compact" in args or "compact" in args
            base = {
                "state": "READY",
                "workspace": str(self.workspace.root_path),
                "workspace_id": "W1",
                "instance_id": self.instance_id,
                "file": self.state_store.data.get("current_file"),
                "line": self.state_store.data.get("line", 1),
                "transaction": self.transaction_id or "NONE",
                "index": "READY" if self.indexer.ready else "WARMING",
                "machine_mode": self.machine,
                "focus": "COMMAND",
            }
            if compact:
                return base
            git = self.git.status() if "GIT" in self.workspace.adapters else None
            return {
                **base,
                "name": self.workspace.name,
                "type": self.workspace.kind,
                "adapters": self.workspace.adapters,
                "dirty": self.state_store.data.get("dirty", False),
                "git": git,
                "process": self.processes.list(),
                "index_details": self.indexer.status(),
            }
        if name in {"pwd", "workspace"}:
            return {"path": str(self.workspace.root_path), "id": "W1", "type": self.workspace.kind}
        if name in {"files", "tree", "projects"}:
            offset, limit = _page_args(args, default_limit=self.page_size)
            items = self.indexer.files(offset=offset, limit=limit)
            items = [{"path": item["path"], "id": self.ids.next("F"), "kind": item["language"],
                      "size": item["size"], "hash": item["hash"][:12]} for i, item in enumerate(items)]
            self.page_items = items; self.page_offset = offset; self.page_size = limit
            self.file_refs.update({item["id"]: item["path"] for item in items})
            if len(self.file_refs) > 1024:
                self.file_refs = dict(list(self.file_refs.items())[-1024:])
            total = self.indexer.count()
            has_more = offset + len(items) < total
            return {"page": offset // limit + 1, "pages": max(1, (total + limit - 1) // limit),
                    "total": total, "returned": len(items), "files": items,
                    "has_more": has_more, "next_offset": offset + limit if has_more else None,
                    "previous_offset": max(0, offset - limit) if offset > 0 else None}
        if name in {"next", "prev"}:
            offset = max(0, self.page_offset + (self.page_size if name == "next" else -self.page_size))
            if self.page_kind == "search" and self.page_query:
                query, extension, path_prefix, regex = self.page_query
                candidate_matches = self.indexer.search(query, extension=extension, path_prefix=path_prefix, regex=regex,
                                                        offset=offset, limit=self.page_size + 1)
                has_more = len(candidate_matches) > self.page_size
                matches = self._register_matches(candidate_matches[:self.page_size])
                self.page_offset = offset; self.page_items = matches
                return {"page": offset // self.page_size + 1, "matches": matches, "returned": len(matches),
                        "has_more": has_more, "next_offset": offset + self.page_size if has_more else None,
                        "previous_offset": max(0, offset - self.page_size) if offset > 0 else None}
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
            start = 1 if name == "tail" and "--count" in args else _int_arg(args, 1, 1)
            count = _option_int(args, "--count", _int_arg(args, 2, 120), minimum=1, maximum=100000)
            if result_ref:
                line = int(result_ref["line"])
                start = max(1, line - 4) if name in {"context", "open"} else line
                count = 12 if name in {"context", "open"} else count
            if name == "tail":
                metadata = self.files.read(args[0], 1, 1)
                tail_start = max(1, int(metadata["total_lines"]) - count + 1)
                info = self.files.read(args[0], tail_start, count)
                self._set_current(args[0], tail_start)
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
            candidate_matches = self.indexer.search(query, extension=extension, path_prefix=path_prefix, regex=regex,
                                                    offset=offset, limit=limit + 1)
            has_more = len(candidate_matches) > limit
            matches = self._register_matches(candidate_matches[:limit])
            self.page_items = matches; self.page_offset = offset; self.page_size = limit
            self.page_kind = "search"; self.page_query = (query, extension, path_prefix, regex)
            if name == "locate":
                radius = _option_int(args, "--context", 2, minimum=0, maximum=20)
                for match in matches:
                    start = max(1, int(match["line"]) - radius)
                    match["context"] = self.files.read(match["file"], start, radius * 2 + 1)["lines"]
                return {"query": query, "count": len(matches), "returned": len(matches), "matches": matches,
                        "has_more": has_more, "next_offset": offset + limit if has_more else None,
                        "previous_offset": max(0, offset - limit) if offset > 0 else None}
            return {"page": offset // limit + 1, "matches": matches, "returned": len(matches),
                    "has_more": has_more, "next_offset": offset + limit if has_more else None,
                    "previous_offset": max(0, offset - limit) if offset > 0 else None}

        if name == "discover-many":
            _require(args, 1, "one or more symbols/search terms")
            limit = _option_int(args, "--limit", 12, minimum=1, maximum=50)
            definitions = _option_int(args, "--definitions", 4, minimum=1, maximum=20)
            references = _option_int(args, "--references", 12, minimum=1, maximum=100)
            extension = _option_value(args, "--ext") or _option_value(args, "--extension")
            path_prefix = _option_value(args, "--path") or _option_value(args, "--path-prefix")
            option_names = {"--limit", "--definitions", "--references", "--ext", "--extension", "--path", "--path-prefix"}
            terms = []
            skip = False
            for value in args:
                if skip:
                    skip = False
                    continue
                if value in option_names:
                    skip = True
                    continue
                if value and value not in terms:
                    terms.append(value)
            if not terms or len(terms) > 32:
                raise ValueError("discover-many requires 1 to 32 terms")
            data = self.indexer.discover_many(
                terms,
                limit_per_term=limit,
                definitions_per_term=definitions,
                references_per_term=references,
                extension=extension,
                path_prefix=path_prefix,
            )
            for bucket in (data.get("by_term") or {}).values():
                if isinstance(bucket, dict) and isinstance(bucket.get("references"), list):
                    bucket["references"] = self._register_matches(bucket["references"])
            return data

        if name == "study":
            _require(args, 1, "symbol or search term")
            term = args[0]
            limit = _option_int(args, "--limit", 8, minimum=1, maximum=20)
            radius = _option_int(args, "--context", 3, minimum=0, maximum=20)
            warnings = []

            try:
                matches = self._register_matches(self.indexer.search(term, limit=limit))
            except Exception as exc:
                matches = []
                warnings.append({
                    "stage": "search",
                    "error": type(exc).__name__,
                    "message": str(exc)[:300],
                })

            for match in matches:
                start = max(1, int(match["line"]) - radius)
                try:
                    match["context"] = self.files.read(
                        match["file"], start, radius * 2 + 1
                    )["lines"]
                except Exception as exc:
                    match["context"] = []
                    warnings.append({
                        "stage": "context",
                        "file": match.get("file"),
                        "line": match.get("line"),
                        "error": type(exc).__name__,
                        "message": str(exc)[:300],
                    })

            try:
                definitions = self.symbol_service.definition(term)
            except Exception as exc:
                definitions = []
                warnings.append({
                    "stage": "definition",
                    "error": type(exc).__name__,
                    "message": str(exc)[:300],
                })

            try:
                references = self._register_matches(
                    self.symbol_service.references(term, min(24, limit * 3))
                )
            except Exception as exc:
                references = []
                warnings.append({
                    "stage": "references",
                    "error": type(exc).__name__,
                    "message": str(exc)[:300],
                })

            files = []
            seen = set()
            for item in [*definitions, *matches, *references]:
                path = item.get("file")
                if path and path not in seen:
                    seen.add(path)
                    files.append(path)

            return {
                "term": term,
                "definitions": definitions[:8],
                "matches": matches,
                "references": references,
                "files": files[:16],
                "index": "incremental",
                "complete": not warnings,
                "warnings": warnings,
            }

        if name == "read-many":
            _require(args, 1, "one or more paths")
            count = _option_int(args, "--count", 240, minimum=1, maximum=2400)
            max_bytes = _option_int(args, "--max-bytes", 2097152, minimum=32768, maximum=8388608)
            paths = []
            skip = False
            option_names = {"--count", "--max-bytes"}
            for item in args:
                if skip:
                    skip = False
                    continue
                if item in option_names:
                    skip = True
                    continue
                paths.append(self._resolve_ref(item))
            if not paths or len(paths) > 64:
                raise ValueError("read-many requires 1 to 64 paths")

            files = []
            skipped = []
            errors = []
            total_bytes = 0
            workers = min(12, len(paths))

            def safe_read(path):
                try:
                    return path, self.files.read(path, 1, count), None
                except Exception as exc:
                    return path, None, {
                        "path": path,
                        "error": str(exc)[:512],
                        "code": str(getattr(exc, "code", "") or type(exc).__name__),
                    }

            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="easychange-read") as pool:
                loaded = list(pool.map(safe_read, paths))
            for position, (path, info, error) in enumerate(loaded):
                if error is not None:
                    errors.append(error)
                    continue
                assert info is not None
                encoded = json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if total_bytes + len(encoded) > max_bytes:
                    if not files:
                        # A single very large read is progressively trimmed so
                        # the optical response remains bounded.
                        lines = list(info.get("lines") or [])
                        while len(encoded) > max_bytes and len(lines) > 1:
                            lines = lines[:max(1, len(lines) // 2)]
                            info = {
                                **info,
                                "lines": lines,
                                "returned_lines": len(lines),
                                "budget_truncated": True,
                                "has_more": True,
                                "next_start": int(info.get("start") or 1) + len(lines),
                            }
                            encoded = json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        files.append(info)
                        total_bytes += len(encoded)
                        skipped.extend(item[0] for item in loaded[position + 1:] if item[2] is None)
                    else:
                        skipped.extend(item[0] for item in loaded[position:] if item[2] is None)
                    break
                files.append(info)
                total_bytes += len(encoded)

            budget_truncated = bool(skipped) or any(bool(item.get("budget_truncated")) for item in files)
            return {
                "files": files,
                "count": len(files),
                "requested": len(paths),
                "raw_bytes": total_bytes,
                "byte_budget": max_bytes,
                "truncated": budget_truncated,
                "skipped": skipped,
                "errors": errors,
                "failed": len(errors),
                "partial": bool(errors) or budget_truncated,
                "has_more": bool(skipped),
                "remaining": len(skipped),
                "parallel_workers": workers,
            }

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
            items = self.history[-(offset+limit):-offset if offset else None]
            has_more = offset + len(items) < len(self.history)
            return {"history": items, "returned": len(items), "total": len(self.history),
                    "has_more": has_more, "next_offset": offset + limit if has_more else None,
                    "previous_offset": max(0, offset - limit) if offset > 0 else None}
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
                    updated = pattern.sub(new, content)
                    self.files.remember(path)
                    self._edit(path, lambda p=path, c=content, u=updated: {
                        **self.files.write(p, u), "_before": c, "_after": u
                    })
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
            compact = "--compact" in args or "--summary" in args
            total = self.journal_store.count
            if compact:
                rendered = self.journal_store.metadata(offset, limit)
            else:
                selected = self.journal_store.slice(offset, offset + limit)
                rendered = [{
                    "change_id": entry.change_id, "session_id": entry.session_id,
                    "timestamp": entry.timestamp, "command": entry.command, "path": entry.path,
                    "before": entry.before, "after": entry.after,
                    "before_hash": entry.before_hash, "after_hash": entry.after_hash,
                    "transaction_id": entry.transaction_id, "source": entry.source,
                } for entry in selected]
            has_more = offset + len(rendered) < total
            return {
                "total": total, "entries": rendered, "returned": len(rendered),
                "compact": compact, "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
                "previous_offset": max(0, offset - limit) if offset > 0 else None,
            }
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
        if name == "locks":
            locks = self.locks.list()
            return {"locks": locks, "count": len(locks)}
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
        if name == "status": return self.git.status(summary="--summary" in args)
        if name == "diff":
            path_arg = next((value for value in args if not value.startswith("--")
                             and (not args or args.index(value) == 0 or args[args.index(value)-1] != "--max-bytes")), None)
            path = self._resolve_ref(path_arg) if path_arg else None
            diff = self.git.diff(path)
            raw = diff.encode("utf-8")
            max_bytes = _option_int(args, "--max-bytes", 0, minimum=0, maximum=8388608)
            truncated = bool(max_bytes and len(raw) > max_bytes)
            if truncated:
                returned = raw[:max_bytes].decode("utf-8", errors="ignore")
            else:
                returned = diff
            return {
                "diff": returned,
                "diff_bytes": len(raw),
                "returned_bytes": len(returned.encode("utf-8")),
                "truncated": truncated,
                "max_bytes": max_bytes or None,
            }
        if name == "branch": return {"branch": self.git.branch(summary="--summary" in args)}
        if name == "log":
            requested = max(1, min(_int_arg(args, 0, 10), 100))
            candidates = self.git.log(min(100, requested + 1))
            has_more = len(candidates) > requested
            commits = candidates[:requested]
            return {"commits": commits, "returned": len(commits), "requested": requested, "has_more": has_more}
        if name == "begin":
            if self.transaction is not None: raise RuntimeError("A transaction is already active")
            self.transaction = []
            self._transaction_dirty_paths.clear()
            self.transaction_id = self.ids.next("TX")
            self._save_transaction()
            self._persist_state()
            return {"transaction": self.transaction_id, "state": "ACTIVE"}
        if name == "commit":
            if self.transaction is None: raise RuntimeError("No active transaction")
            count = len(self.transaction)
            journal_ids = []
            for change in self.transaction:
                entry = self.journal_store.record(change.command, change.path, change.before, change.after, self.transaction_id)
                change.change_id = entry.change_id
                journal_ids.append(entry.change_id)
                self._remember_journal_metadata(change)
            self.transaction = None; self.transaction_id = None
            self._clear_transaction()
            indexed = self._flush_transaction_index()
            self._persist_state()
            return {"committed_changes": count, "indexed_paths": indexed, "journal_ids": journal_ids}
        if name == "rollback":
            if self.transaction is None: raise RuntimeError("No active transaction")
            changes = list(reversed(self.transaction))
            for change in changes: self._restore(change)
            self.transaction = None; self.transaction_id = None
            self._clear_transaction()
            indexed = self._flush_transaction_index()
            self._persist_state()
            return {"rolled_back": len(changes), "indexed_paths": indexed}
        if name == "undo":
            entry_id = args[0] if args else None
            entry = self.journal_store.get(entry_id)
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
            self._apply_after(change); self._remember_journal_metadata(change)
            return {"redone": change.path, "change_id": change.change_id}
        if name == "runtime-profile":
            if args:
                return {"profile": self.runtime.profile(args[0]), "profiles": self.runtime.profiles()}
            return {"profiles": self.runtime.profiles()}
        if name == "run-project":
            profile = args[0] if args and not args[0].startswith("--") else ""
            process_id = self.ids.next("P")
            return self.runtime.start(process_id, profile)
        if name == "test-smart":
            profile = args[0] if args and not args[0].startswith("--") else ""
            timeout = _option_int(args, "--timeout", 600, minimum=5, maximum=3600)
            result = self.runtime.smart_test(profile, timeout)
            if not result.get("passed"):
                raise ProcessFailure(Result(False, "test-smart", data=result, error="Smart test failed", code="TEST_FAILED"))
            return result
        if name == "test-evidence":
            profile = _option_value(args, "--profile", "")
            timeout = _option_int(args, "--timeout", 600, minimum=5, maximum=3600)
            title = _option_value(args, "--title", "test-evidence")
            data = self.runtime.test_with_evidence(profile, timeout, title)
            if not data.get("passed"):
                raise ProcessFailure(Result(False, "test-evidence", data=data, error="Test evidence failed", code="TEST_FAILED"))
            return data
        if name == "process-logs":
            _require(args, 1, "process ID")
            return self.processes.logs(args[0], limit=_option_int(args, "--limit", 12000, minimum=512, maximum=100000))
        if name == "swagger-evidence":
            profile = _option_value(args, "--profile", "")
            url = _option_value(args, "--url", "")
            timeout = float(_option_value(args, "--timeout", "45") or 45)
            process_id = self.ids.next("P")
            return self.runtime.swagger_evidence(
                process_id, profile, url, timeout,
                "--no-screenshot" not in args, "--no-start" not in args,
                "--keep-running" in args
            )
        if name == "evidence":
            title = _option_value(args, "--title", "test-evidence")
            process_id = _option_value(args, "--process", "")
            return self.runtime.evidence(title, process_id)
        if name == "db-connections":
            return self.database.connections()
        if name == "db-schema":
            _require(args, 1, "connection name")
            return self.database.schema(args[0], max_rows=_option_int(args, "--max-rows", 500, minimum=1, maximum=5000))
        if name == "db-query":
            _require(args, 2, "connection name and SQL")
            connection = args[0]
            filtered = []
            skip = False
            for index, value in enumerate(args[1:], 1):
                if skip:
                    skip = False
                    continue
                if value in {"--max-rows", "--timeout"}:
                    skip = True
                    continue
                if value == "--write":
                    continue
                filtered.append(value)
            sql = " ".join(filtered)
            return self.database.query(
                connection, sql, allow_write="--write" in args,
                max_rows=_option_int(args, "--max-rows", 200, minimum=1, maximum=5000),
                timeout=_option_int(args, "--timeout", 30, minimum=1, maximum=300),
            )
        if name == "validate":
            include_test = "--test" in args or "test" in args
            diff = self.git.diff() if "GIT" in self.workspace.adapters else ""
            build = self.execute_tokens("build", [], raw=":build")
            if not build.ok:
                raise ProcessFailure(Result(False, "validate", data={"diff": diff, "build": build.to_dict(), "test": None},
                                            error=build.error, code=build.code or "BUILD_FAILED"))
            test_result = None
            if include_test:
                # validate has just completed a successful .NET build, so do
                # not make dotnet test rebuild the same solution again.
                test_args = ["dotnet", "test", "--no-build", "--no-restore"] if self.workspace.kind == "DOTNET" else []
                test_result = self.execute_tokens("test", test_args, raw=":test")
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
        if name == "processes":
            processes = self.processes.list()
            running = sum(1 for item in processes if str(item.get("state") or "").upper() == "RUNNING")
            return {"processes": processes, "count": len(processes), "running": running,
                    "terminal": len(processes) - running}
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
        self._index_changed_path(relative)
        return outcome

    def _force_write(self, path: str, content: str) -> dict:
        resolved = self.workspace.resolve(path)
        relative = resolved.relative_to(self.workspace.root_path).as_posix()
        self.locks.check_write(relative, self.instance_id, self.session_id)
        before = resolved.read_text(encoding="utf-8") if resolved.exists() else None
        if resolved.exists(): self.files.remember(relative)
        outcome = self.files.write(path, content, force=True)
        self._record(relative, before, content)
        self._index_changed_path(relative)
        return outcome

    def _edit(self, path: str, operation) -> dict:
        resolved = self.workspace.resolve(path, must_exist=True)
        relative = resolved.relative_to(self.workspace.root_path).as_posix()
        self.locks.check_write(relative, self.instance_id, self.session_id)
        outcome = operation()
        if not isinstance(outcome, dict) or "_before" not in outcome or "_after" not in outcome:
            raise RuntimeError("EDIT_OPERATION_SNAPSHOT_REQUIRED")
        before = outcome.pop("_before")
        after = outcome.pop("_after")
        self._record(relative, before, after)
        self._index_changed_path(relative)
        return outcome

    def _index_changed_path(self, path: str) -> None:
        if self.transaction is not None:
            self._transaction_dirty_paths.add(path)
        else:
            self.indexer.update_path(path)

    def _flush_transaction_index(self) -> int:
        if not self._transaction_dirty_paths:
            return 0
        paths = sorted(self._transaction_dirty_paths)
        self._transaction_dirty_paths.clear()
        for path in paths:
            self.indexer.update_path(path)
        return len(paths)
    def _record(self, path: str, before: str | None, after: str | None) -> None:
        change = Change(path, before, after, command=self.last_command or "edit")
        if self.transaction is not None:
            self.transaction.append(change)
            self._save_transaction()
        else:
            entry = self.journal_store.record(change.command, path, before, after)
            change.change_id = entry.change_id
            self._remember_journal_metadata(change)

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
        if not self.transaction_path.exists():
            return
        try:
            value = json.loads(self.transaction_path.read_text(encoding="utf-8"))
            self.transaction_id = value["transaction_id"]
            if value.get("format") == "append-v2":
                changes: list[Change] = []
                if self.transaction_log_path.exists():
                    for line in self.transaction_log_path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        try:
                            item = json.loads(line)
                            changes.append(Change(**item))
                        except (json.JSONDecodeError, TypeError):
                            # A crash can leave only the final append incomplete.
                            # Never invent a change from a partial record.
                            break
                self.transaction = changes
                self._transaction_persisted_count = len(changes)
                self._transaction_storage_v2 = True
            else:
                # Backward compatibility with the original monolithic JSON
                # transaction file. The next save migrates it to append-v2.
                self.transaction = [Change(**item) for item in value.get("changes", [])]
                self._transaction_persisted_count = 0
                self._transaction_storage_v2 = False
        except (OSError, ValueError, KeyError, TypeError):
            self.transaction_id = None
            self.transaction = None
            self._transaction_persisted_count = 0
            self._transaction_storage_v2 = False

    def _save_transaction(self) -> None:
        """Persist active transaction in O(new changes), not O(total changes)."""
        if self.transaction is None:
            return
        self.transaction_path.parent.mkdir(parents=True, exist_ok=True)

        if not self._transaction_storage_v2:
            temporary = self.transaction_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"format": "append-v2", "transaction_id": self.transaction_id},
                    ensure_ascii=False, separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            temporary.replace(self.transaction_path)
            self.transaction_log_path.unlink(missing_ok=True)
            self._transaction_persisted_count = 0
            self._transaction_storage_v2 = True

        if self._transaction_persisted_count >= len(self.transaction):
            return

        with self.transaction_log_path.open("a", encoding="utf-8", newline="\n") as stream:
            for change in self.transaction[self._transaction_persisted_count:]:
                stream.write(json.dumps({
                    "path": change.path,
                    "before": change.before,
                    "after": change.after,
                    "change_id": change.change_id,
                    "command": change.command,
                }, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._transaction_persisted_count = len(self.transaction)

    def _clear_transaction(self) -> None:
        self.transaction_path.unlink(missing_ok=True)
        self.transaction_log_path.unlink(missing_ok=True)
        self._transaction_persisted_count = 0
        self._transaction_storage_v2 = False

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
            self.snapshots[snapshot_id] = self.journal_store.count
            self._persist_state()
            return {"snapshot": snapshot_id, "changes": self.journal_store.count}
        if args[0] == "restore":
            _require(args, 2, "restore and snapshot ID")
            snapshot_id = args[1]
            if snapshot_id not in self.snapshots: raise LookupError(f"Snapshot not found: {snapshot_id}")
            entries = self.journal_store.slice(self.snapshots[snapshot_id])
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
        result = self.results.get(value)
        if isinstance(result, dict) and result.get("file"):
            return str(result["file"])
        return self.file_refs.get(value, value)

    def _register_matches(self, matches: list[dict]) -> list[dict]:
        registered = []
        for value in matches:
            item = dict(value)
            item["id"] = self.ids.next("R")
            # Persist only navigation metadata. The returned object may later
            # receive large context arrays for study/locate, but those are
            # transport payloads, not session history.
            stored = {
                key: item[key]
                for key in ("id", "file", "line", "text", "score")
                if key in item
            }
            self.results[item["id"]] = stored
            registered.append(item)

        if len(self.results) > 512:
            self.results = dict(list(self.results.items())[-512:])
        if len(self.file_refs) > 1024:
            self.file_refs = dict(list(self.file_refs.items())[-1024:])
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


def _option_value(args: list[str], name: str, default: str = "") -> str:
    for index, value in enumerate(args[:-1]):
        if value == name:
            return str(args[index + 1])
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
