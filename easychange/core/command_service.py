from __future__ import annotations
from .runtime_service import ProjectRuntimeService
from .database_service import DatabaseService
from .command_metadata import COMMAND_NAMES, command_schema
from .ef_migration_service import EfMigrationService
from concurrent.futures import ThreadPoolExecutor
from .safe_edit import SafeEditError, plan_file_mutations, text_hash
from .result_store import DurableResultStore
from .secret_broker import SecretBroker
from .browser_service import BrowserService
from .ubuntu_service import UbuntuService
from .inbound_transfer import InboundCommandStore, InboundTransferError

import shlex
import time
import re
import threading
import json
import os
import tempfile
import secrets
import binascii
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

from .command_parser import CommandParser
from .file_service import ExternalChangeError, FileService, MAX_TEXT_FILE_BYTES
from .git_service import GitService
from .human_text_service import HumanTextService
from .indexer import Indexer
from .ids import IdFactory
from .journal import Journal
from .locks import LockManager
from .maintenance_service import MaintenanceService
from .process_service import ProcessService
from .operation_registry import (
    STRUCTURED_CONTINUE_SAFE_TYPES, STRUCTURED_JOB_TYPES, STRUCTURED_MUTATION_TYPES,
    STRUCTURED_RECOVERY_TYPES, TEXT_MUTATION_COMMANDS, structured_capabilities,
    structured_operation_is_mutation,
)
from .result import Result
from .transport_codec import decode_command
from .state import MachineSession
from .symbol_service import SymbolService
from .workspace import Workspace
from .watcher import WorkspaceWatcher
from ..remote.agent_profile import REMOTE_PROFILE, remote_guide_markdown
from .. import __version__


# Defensive restart budgets: normal edited text is capped at 4 MiB by
# FileService; allow generous headroom for legacy JSON escaping/snapshots.
MAX_ACTIVE_TRANSACTION_HEADER_BYTES = 64 * 1024 * 1024
# Enforce a generous command envelope *before* strip/casefold/regex or
# chained parser allocations. Full-file transfers have their own staged path.
MAX_COMMAND_INPUT_CHARS = 32 * 1024 * 1024
MAX_ACTIVE_TRANSACTION_RECORD_CHARS = 64 * 1024 * 1024


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


def _unique_structured_fields(pairs):
    """Reject ambiguous J1 payloads before deduplication or mutation planning."""
    value={}
    for key,item in pairs:
        if key in value:
            raise ValueError("DUPLICATE_STRUCTURED_JSON_KEY")
        value[key]=item
    return value


def _finite_structured_float(token):
    number=float(token)
    if not math.isfinite(number):
        raise ValueError("NONFINITE_STRUCTURED_JSON_NUMBER")
    return number


def _reject_structured_constant(_token):
    raise ValueError("NONFINITE_STRUCTURED_JSON_NUMBER")


def _decode_structured_payload(raw):
    return json.loads(
        raw, object_pairs_hook=_unique_structured_fields,
        parse_float=_finite_structured_float,
        parse_constant=_reject_structured_constant,
    )


class CommandService:
    """Thread-safe deterministic control plane for CLI diagnostics and HID-driven Machine GUI."""
    def __init__(
        self,
        workspace: Workspace,
        *,
        defer_initial_maintenance: bool = False,
    ) -> None:
        self.workspace = workspace
        self.state_store = MachineSession(workspace.root_path / ".easychange" / "session.json", str(workspace.root_path))
        self.session_id = self.state_store.data["session_id"]
        self.instance_id = "A" + secrets.token_hex(3).upper()
        self.files = FileService(workspace, self.state_store.data.get("file_hashes", {}))
        self.git = GitService(workspace)
        self.processes = ProcessService(workspace.root_path)
        self.browser = BrowserService(self.processes)
        self.ubuntu = UbuntuService(self.processes)
        self.human_text = HumanTextService(self.processes.workspace_state_root)
        self.secret_broker = SecretBroker()
        self.runtime = ProjectRuntimeService(
            workspace,
            self.processes,
            external_env_provider=self.secret_broker.values,
        )
        self.database = DatabaseService(
            workspace,
            external_env_provider=self.secret_broker.values,
        )
        self.ef_migrations = EfMigrationService(workspace, self.processes, self.runtime, self.database)
        self.ids = IdFactory(self.state_store.data.get("id_counts"))
        self.output = self.state_store.data.get("output_mode", "text")
        self.machine = self.state_store.data.get("mode", "human") == "machine"
        self.history: list[str] = list(self.state_store.data.get("history", []))[-50:]
        self.aliases = dict(self.state_store.data.get("aliases", {}))
        self.parser = CommandParser(self.aliases)
        self.indexer = Indexer(workspace.root_path)
        for sensitive_path in self.state_store.data.get("sensitive_source_paths", []):
            try:
                # The plaintext was scrubbed when the source was first
                # imported. Startup only has to keep the path excluded; running
                # VACUUM once per persisted secret path made session startup scale
                # poorly for no additional security benefit.
                self.indexer.protect_path(str(sensitive_path), scrub=False)
            except (OSError, ValueError):
                pass
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
        self.inbound_transfers = InboundCommandStore(
            workspace.root_path / ".easychange" / "inbound"
        )
        self.maintenance = MaintenanceService(workspace.root_path)
        self._maintenance_interval_seconds = 300.0
        self._maintenance_last = 0.0
        self._maintenance_report: dict = {}
        self._maintenance_guard = threading.RLock()
        self._maintenance_thread: threading.Thread | None = None
        if defer_initial_maintenance:
            # GUI startup treats automatic cleanup as idle/background work. Mark
            # it scheduled so the first command does not synchronously perform
            # the same scan before the background worker starts.
            self._maintenance_last = time.monotonic()
            self._maintenance_report = {
                "mode": "automatic",
                "pending": True,
                "deferred": True,
            }
        else:
            self._run_maintenance(force=True)

    def _run_maintenance(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        scan_workspace_temps: bool = False,
    ) -> dict:
        now = time.monotonic()
        if (
            not force
            and self._maintenance_report
            and now - self._maintenance_last < self._maintenance_interval_seconds
        ):
            return dict(self._maintenance_report)

        with self._maintenance_guard:
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
            report["pending"] = False
            self._maintenance_last = time.monotonic()
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

    def start_deferred_maintenance(self) -> bool:
        """Run the initial automatic cleanup off the caller/UI thread once."""
        current = self._maintenance_thread
        if current is not None and current.is_alive():
            return False

        def worker() -> None:
            try:
                self._run_maintenance(force=True)
            except (OSError, ValueError, RuntimeError) as exc:
                with self._maintenance_guard:
                    self._maintenance_last = time.monotonic()
                    self._maintenance_report = {
                        "mode": "automatic",
                        "pending": False,
                        "deferred": True,
                        "error": type(exc).__name__,
                    }

        thread = threading.Thread(
            target=worker,
            name="easychange-maintenance",
            daemon=True,
        )
        self._maintenance_thread = thread
        thread.start()
        return True
    def close(self) -> None:
        maintenance_thread = self._maintenance_thread
        if (
            maintenance_thread is not None
            and maintenance_thread.is_alive()
            and maintenance_thread is not threading.current_thread()
        ):
            maintenance_thread.join(timeout=2.0)
        if self.watcher:
            self.watcher.stop()
        self.processes.stop_all()
        self.secret_broker.clear()
        self.indexer.close()
        self.locks.release_owner(self.instance_id, self.session_id)
        self._persist_state(self.last_result)

    def _execute_inbound_upload_control(
        self,
        command: str,
        *,
        sequence: str | None = None,
    ) -> Result:
        raw = str(command or "").strip()
        parts = raw.split()
        suppress = False
        try:
            if len(parts) < 3 or parts[0].casefold() != ":u1":
                raise InboundTransferError("INVALID_INBOUND_UPLOAD_COMMAND")
            action = parts[1].casefold()
            if action == "begin":
                if len(parts) != 7:
                    raise InboundTransferError("INVALID_INBOUND_BEGIN")
                data = self.inbound_transfers.begin(
                    parts[2],
                    raw_bytes=int(parts[3]),
                    compressed_bytes=int(parts[4]),
                    chunk_count=int(parts[5]),
                    sha256=parts[6],
                )
                suppress = True
            elif action == "chunk":
                chunk_parts = raw.split(" ", 5)
                if len(chunk_parts) != 6:
                    raise InboundTransferError("INVALID_INBOUND_CHUNK")
                data = self.inbound_transfers.put_chunk(
                    chunk_parts[2],
                    index=int(chunk_parts[3]),
                    crc32=chunk_parts[4],
                    token=chunk_parts[5],
                )
                suppress = True
            elif action == "status":
                if len(parts) != 3:
                    raise InboundTransferError("INVALID_INBOUND_STATUS")
                data = self.inbound_transfers.status(parts[2])
            elif action == "abort":
                if len(parts) != 3:
                    raise InboundTransferError("INVALID_INBOUND_ABORT")
                data = self.inbound_transfers.abort(parts[2])
                suppress = True
            elif action == "commit":
                raise InboundTransferError("INBOUND_COMMIT_REQUIRES_SEQUENCE")
            else:
                raise InboundTransferError("INVALID_INBOUND_UPLOAD_ACTION")

            if suppress:
                data = {**data, "suppress_optical_broadcast": True}
            return Result(
                True,
                "u1",
                data=data,
                command_id=self.ids.next("C"),
                sequence=sequence,
            )
        except (TypeError, ValueError, InboundTransferError) as exc:
            code = (
                exc.code
                if isinstance(exc, InboundTransferError)
                else "INVALID_INBOUND_UPLOAD_COMMAND"
            )
            data = dict(exc.data) if isinstance(exc, InboundTransferError) else {}
            if suppress or (
                len(parts) >= 2
                and parts[1].casefold() in {"begin", "chunk", "abort"}
            ):
                data["suppress_optical_broadcast"] = True
            return Result(
                False,
                "u1",
                data=data,
                error=str(exc),
                code=code,
                command_id=self.ids.next("C"),
                sequence=sequence,
            )
    def execute(self, command: str) -> Result:
        if not isinstance(command, str):
            return Result(False, "", error="Command must be text", code="INVALID_COMMAND")
        if len(command) > MAX_COMMAND_INPUT_CHARS:
            return Result(False, "", error="Command exceeds input budget",
                          code="COMMAND_INPUT_TOO_LARGE")
        raw = command.strip()
        if not raw: return Result(False, "", error="Empty command", code="INVALID_COMMAND")
        if raw.casefold().startswith(":u1 "):
            return self._execute_inbound_upload_control(raw)
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
        inbound_upload = None
        if command.casefold().startswith(":u1 status "):
            return self._execute_inbound_upload_control(command, sequence=sequence)
        if command.casefold().startswith(":u1 commit "):
            parts = command.split()
            if len(parts) != 3:
                return Result(
                    False,
                    "u1",
                    error="INVALID_INBOUND_COMMIT",
                    code="INVALID_INBOUND_COMMIT",
                    sequence=sequence,
                )
            try:
                command, inbound_upload = self.inbound_transfers.assemble(parts[2])
            except InboundTransferError as exc:
                return Result(
                    False,
                    "u1",
                    data=dict(exc.data),
                    error=str(exc),
                    code=exc.code,
                    sequence=sequence,
                )
        if command.casefold().startswith(":z1 "):
            try:
                command, codec = decode_command(command)
            except ValueError as exc:
                return Result(False, "ec", error=str(exc), code="INVALID_COMPRESSED_PAYLOAD", sequence=sequence)
        external_env: dict[str, str] | None = None
        if command.casefold().startswith(":j1 "):
            try:
                structured = _decode_structured_payload(command[4:])
                if not isinstance(structured, dict):
                    raise ValueError("EC1_J1_OBJECT_REQUIRED")
                if "_external_env" in structured:
                    raise ValueError("PLAINTEXT_EXTERNAL_ENV_FORBIDDEN_USE_SECRET_BROKER")
                command = json.dumps(structured, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            except (json.JSONDecodeError, ValueError) as exc:
                return Result(False, "ec", error=str(exc), code="INVALID_STRUCTURED_PAYLOAD", sequence=sequence)

        try:
            digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
        except UnicodeEncodeError:
            # JSON permits escaped lone surrogates, but they cannot be
            # serialized on the UTF-8 wire or used as a durable command hash.
            # Reject before claiming a sequence or touching the filesystem.
            return Result(
                False, "ec", error="Command contains invalid UTF-8",
                code="INVALID_COMMAND_ENCODING", sequence=sequence)
        mutation_types = STRUCTURED_MUTATION_TYPES
        if structured is not None:
            structured_op = str(structured.get("op") or "").casefold()
            structured_items = structured.get("operations") if structured_op == "batch" else [structured]
            is_mutation = any(
                structured_operation_is_mutation(item)
                for item in (structured_items if isinstance(structured_items, list) else [])
                if isinstance(item, dict)
            )
        else:
            first = command.strip().split(maxsplit=1)[0].lstrip(":").casefold() if command.strip() else ""
            is_mutation = first in TEXT_MUTATION_COMMANDS

        # Durable store is authoritative for replay. A lost HDMI frame or MCP
        # restart therefore never requires a mutating command to execute twice.
        def _reuse_durable_receipt(durable: dict[str, Any]) -> Result:
            if str(durable.get("command_hash") or "") != digest:
                return Result(
                    False, "ec", error="Sequence reused with different command",
                    code="SEQUENCE_CONFLICT", sequence=sequence,
                )
            if durable.get("state") == "DONE" and durable.get("result_expired") is True:
                # The full result was compacted after TTL, but its durable
                # sequence receipt still prohibits any second execution.
                return Result(
                    False, "ec",
                    error="Result payload expired; sequence already executed",
                    code="SEQUENCE_RESULT_EXPIRED", sequence=sequence,
                    data={"state":"DONE","duplicate":True,
                          "result_hash":durable.get("expired_result_hash")},
                )
            if durable.get("state") == "DONE" and isinstance(durable.get("result"), dict):
                # Imported or corrupt terminal records must never crash the
                # replay path or silently turn into permission to re-execute.
                try:
                    replay = Result(**durable["result"])
                    if (type(replay.ok) is not bool
                            or not isinstance(replay.command, str)
                            or not isinstance(replay.data, dict)):
                        raise TypeError("MALFORMED_DURABLE_RESULT")
                except (TypeError, ValueError):
                    replay = None
                if replay is not None:
                    replay.sequence = sequence
                    replay.data = {
                        **replay.data,
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
            if durable.get("state") == "DONE":
                # An incomplete or legacy terminal receipt is not a running
                # command and must never authorize another filesystem mutation.
                # Explicitly surface missing replay data rather than asking
                # callers to poll RUNNING forever.
                return Result(
                    False, "ec",
                    error="Completed sequence has no replayable result",
                    code="SEQUENCE_RESULT_MISSING", sequence=sequence,
                    data={"state": "DONE", "duplicate": True},
                )
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
        def _claim_durable() -> tuple[dict[str, Any] | None, Result | None]:
            try:
                record, acquired = self.result_store.claim_running(
                    sequence, digest, command, mutation=is_mutation)
            except ValueError as exc:
                if str(exc) == "RESULT_SEQUENCE_COMMAND_HASH_MISMATCH":
                    return None, Result(
                        False, "ec", error="Sequence reused with different command",
                        code="SEQUENCE_CONFLICT", sequence=sequence)
                raise
            if not acquired:
                return None, _reuse_durable_receipt(record)
            return record, None

        durable = self.result_store.get(sequence)
        if durable is not None:
            return _reuse_durable_receipt(durable)

        # Preserve compatibility with the compact legacy session journal while
        # migrating successful replays to the durable store.
        with self._guard:
            # A torn/legacy session journal must not turn an unknown prior
            # execution into permission to repeat a filesystem mutation.
            # Durable receipts are checked first, so refuse only when this
            # fallback journal itself cannot be interpreted safely.
            legacy_root = self.state_store.data.get("ec_results", {})
            if not isinstance(legacy_root, dict):
                return Result(
                    False, "ec", error="Legacy execution journal is unreadable",
                    code="SEQUENCE_STATE_UNAVAILABLE", sequence=sequence)
            journal = dict(legacy_root)
            previous = journal.get(sequence)
            if previous is not None and not isinstance(previous, dict):
                return Result(
                    False, "ec", error="Legacy sequence entry is unreadable",
                    code="SEQUENCE_STATE_UNAVAILABLE", sequence=sequence)
            if previous:
                if previous.get("sha256") != digest:
                    return Result(False, "ec", error="Sequence reused with different command",
                                  code="SEQUENCE_CONFLICT", sequence=sequence)
                cached = self._ec_result_cache.get(sequence)
                if previous.get("state") == "DONE" and cached is not None:
                    # Cached legacy state can be malformed after an older
                    # runtime upgrade; never treat a broken DONE record as
                    # authority to execute or rewrite the same mutation.
                    try:
                        replay = Result(**cached.to_dict())
                        if (type(replay.ok) is not bool
                                or not isinstance(replay.command,str)
                                or not isinstance(replay.data,dict)):
                            raise ValueError("MALFORMED_CACHED_LEGACY_RESULT")
                    except (AttributeError,TypeError,ValueError,RecursionError):
                        return Result(
                            False, "ec",
                            error="Completed sequence has no replayable result",
                            code="SEQUENCE_RESULT_MISSING", sequence=sequence,
                            data={"state":"DONE","duplicate":True},
                        )
                    replay.sequence = sequence
                    replay.data = {**replay.data, "duplicate": True}
                    receipt = replay.data.get("mutation_receipt")
                    _record, refusal = _claim_durable()
                    if refusal is not None:
                        return refusal
                    self.result_store.complete(
                        sequence, digest, command, replay.to_dict(),
                        mutation_receipt=receipt if isinstance(receipt, dict) else None,
                    )
                    return replay
                legacy = previous.get("result")
                if previous.get("state") == "DONE" and isinstance(legacy, dict):
                    # A journal is legacy/untrusted state. Malformed replay
                    # objects must not escape as Python exceptions, nor
                    # authorize a second filesystem mutation.
                    try:
                        replay = Result(**legacy)
                        if (type(replay.ok) is not bool
                                or not isinstance(replay.command, str)
                                or not isinstance(replay.data, dict)):
                            raise ValueError("MALFORMED_LEGACY_RESULT")
                    except (TypeError, ValueError):
                        return Result(
                            False, "ec",
                            error="Completed sequence has no replayable result",
                            code="SEQUENCE_RESULT_MISSING", sequence=sequence,
                            data={"state": "DONE", "duplicate": True},
                        )
                    replay.sequence = sequence
                    replay.data = {**replay.data, "duplicate": True}
                    receipt = replay.data.get("mutation_receipt")
                    _record, refusal = _claim_durable()
                    if refusal is not None:
                        return refusal
                    self.result_store.complete(
                        sequence, digest, command, replay.to_dict(),
                        mutation_receipt=receipt if isinstance(receipt, dict) else None,
                    )
                    return replay
                if previous.get("state") == "DONE":
                    # A legacy DONE journal without replay data is evidence of
                    # prior execution, not permission to run the mutation again.
                    return Result(
                        False, "ec", error="Completed sequence has no replayable result",
                        code="SEQUENCE_RESULT_MISSING", sequence=sequence)
                if previous.get("state") != "DONE":
                    _record, refusal = _claim_durable()
                    if refusal is not None:
                        return refusal
                    return Result(
                        False, "ec",
                        error="Execution is already marked RUNNING; mutation will not be replayed blindly",
                        code="RUNNING",
                        data={"state": previous.get("state", "RUNNING"), "mutation": is_mutation},
                        sequence=sequence,
                    )

            # Reading an empty spool earlier did not reserve this sequence:
            # another EasyChange process may have claimed it in the meantime.
            # Only the winner of the durable cross-process claim may execute.
            _record, refusal = _claim_durable()
            if refusal is not None:
                return refusal

            journal[sequence] = {
                "sha256": digest,
                "state": "RUNNING",
                "command": command[:256],
                "mutation": is_mutation,
                "started_at": time.time(),
            }
            self.state_store.data["ec_results"] = dict(list(journal.items())[-128:])
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
            result = (
                self._execute_structured(
                    structured,
                    persist=False,
                    sequence=sequence,
                    external_env=external_env,
                )
                if structured is not None
                else self.execute(command)
            )
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
        if inbound_upload:
            result.data = {
                **(result.data or {}),
                "inbound_upload": dict(inbound_upload),
            }

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
        result_value = result.to_wire_dict()
        stored = self.result_store.complete(
            sequence,
            digest,
            command,
            result_value,
            mutation_receipt=receipt if isinstance(receipt, dict) else None,
        )
        serialized_bytes = int(stored.get("result_bytes") or 0)

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
                "result_hash": stored.get("result_hash"),
                "finished_at": stored.get("finished_at"),
            }
            self.state_store.data["ec_results"] = dict(list(journal.items())[-128:])
            self._remember_ec_result(sequence, result, serialized_bytes)
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
                        # FileService enforces this existing 4 MiB text
                        # limit for writes; the planned-batch preflight must
                        # enforce it *before* materializing a source file.
                        with resolved.open("rb") as stream:
                            data = stream.read(MAX_TEXT_FILE_BYTES + 1)
                        if len(data) > MAX_TEXT_FILE_BYTES:
                            raise SafeEditError(
                                "FILE_TOO_LARGE",
                                f"File exceeds text read limit ({MAX_TEXT_FILE_BYTES} bytes): {relative}",
                            )
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
        # Keep only one worker-sized batch of region bodies in flight, rather
        # than materializing all 64 full-text reads before budgeting output.
        def bounded_reads():
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="easychange-regions") as pool:
                for offset in range(0, len(normalized), workers):
                    yield from pool.map(safe_read, normalized[offset:offset + workers])

        loaded = bounded_reads()
        max_bytes = max(32768, min(8388608, int(max_bytes)))
        output: list[dict] = []
        skipped: list[dict] = []
        errors: list[dict] = []
        total_bytes = 0
        try:
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
                        if len(encoded) > max_bytes:
                            # A single pathological/minified line can be megabytes.
                            # Never violate the optical response budget just because
                            # line-based paging cannot split it safely.
                            errors.append({
                                "path": spec["path"],
                                "start": spec["start"],
                                "count": spec["count"],
                                "error": "Single source line exceeds read_regions byte budget",
                                "code": "LINE_EXCEEDS_BYTE_BUDGET",
                                "byte_budget": max_bytes,
                            })
                        else:
                            output.append(value)
                            total_bytes += len(encoded)
                            consumed = len(lines)
                            remaining_count = max(0, int(spec["count"]) - consumed)
                            if bool(value.get("budget_truncated")) and remaining_count > 0:
                                skipped.append({
                                    "path": spec["path"],
                                    "start": int(spec["start"]) + consumed,
                                    "count": remaining_count,
                                })
                        skipped.extend(normalized[position + 1:])
                    else:
                        skipped.extend(normalized[position:])
                    break
                output.append(value)
                total_bytes += len(encoded)

        finally:
            # Closing the generator releases the worker pool after the
            # current bounded batch, without submitting unread later batches.
            loaded.close()

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

    def _resolve_browser_secret_refs(self, value):
            sources = self.secret_broker.values()
            used: set[str] = set()
            template_pattern = re.compile(r"\$\{SECRET:([A-Za-z_][A-Za-z0-9_]*)\}")
            uri_pattern = re.compile(r"secret://([A-Za-z_][A-Za-z0-9_]*)")

            def resolve_name(name: str) -> str:
                if name not in sources:
                    raise KeyError(f"SECRET_REF_NOT_FOUND:{name}")
                used.add(name)
                return sources[name]

            def visit(item):
                if isinstance(item, dict):
                    if set(item) == {"$secret_ref"}:
                        return resolve_name(str(item.get("$secret_ref") or ""))
                    return {key: visit(child) for key, child in item.items()}
                if isinstance(item, list):
                    return [visit(child) for child in item]
                if isinstance(item, str):
                    output = template_pattern.sub(
                        lambda match: resolve_name(match.group(1)),
                        item,
                    )
                    output = uri_pattern.sub(
                        lambda match: resolve_name(match.group(1)),
                        output,
                    )
                    return output
                return item

            return visit(value), sorted(used), sources

    @staticmethod
    def _scrub_browser_secret_values(value, sources: dict[str, str]):
        secret_values = [
            str(secret)
            for secret in sources.values()
            if isinstance(secret, str) and secret
        ]
        if isinstance(value, str):
            output = value
            for secret in secret_values:
                output = output.replace(secret, "[REDACTED]")
            return output
        if isinstance(value, list):
            return [
                CommandService._scrub_browser_secret_values(item, sources)
                for item in value
            ]
        if isinstance(value, dict):
            return {
                key: CommandService._scrub_browser_secret_values(item, sources)
                for key, item in value.items()
            }
        return value
    def _execute_ubuntu_operation(self, item: dict) -> Result:
        kind = str(item.get("type") or "").casefold()
        secret_sources: dict[str, str] = {}
        used_secret_refs: list[str] = []
        started = time.monotonic()
        try:
            item, used_secret_refs, secret_sources = self._resolve_browser_secret_refs(item)
            kind = str(item.get("type") or "").casefold()
            session_id = str(item.get("session_id") or "hml")
            wait_ms = max(0, min(10000, int(item.get("wait_ms") or 1800)))
            max_bytes = max(1024, min(1024 * 1024, int(item.get("max_bytes") or 65536)))

            if kind == "ubuntu_open":
                data = self.ubuntu.open(
                    session_id,
                    distro=str(item.get("distro") or ""),
                    cwd=str(item.get("cwd") or "/mnt/c"),
                    env=item.get("env") if isinstance(item.get("env"), dict) else {},
                    kubeconfig=str(item.get("kubeconfig") or ""),
                    namespace=str(item.get("namespace") or "hml"),
                    context=str(item.get("context") or ""),
                    environment=str(item.get("environment") or "hml"),
                    verify_kubectl=bool(item.get("verify_kubectl", True)),
                )
            elif kind == "ubuntu_state":
                data = self.ubuntu.state(session_id, probe=bool(item.get("probe", False)))
            elif kind == "ubuntu_close":
                data = self.ubuntu.close(session_id)
            elif kind == "ubuntu_cd":
                data = self.ubuntu.cd(session_id, str(item.get("cwd") or ""))
            elif kind == "ubuntu_env":
                data = self.ubuntu.update_env(
                    session_id,
                    set_values=item.get("set") if isinstance(item.get("set"), dict) else {},
                    unset=item.get("unset") if isinstance(item.get("unset"), list) else [],
                )
            elif kind == "ubuntu_exec":
                argv = item.get("argv")
                if not isinstance(argv, list):
                    raise ValueError("UBUNTU_ARGV_REQUIRES_1_TO_64_STRINGS")
                data = self.ubuntu.exec(
                    session_id,
                    [str(value) for value in argv],
                    job_id=str(item.get("job_id") or self.ids.next("J")),
                    wait_ms=wait_ms,
                    max_bytes=max_bytes,
                    allow_mutation=item.get("allow_mutation", False),
                    confirm_context=str(item.get("confirm_context") or ""),
                )
            elif kind == "ubuntu_shell":
                data = self.ubuntu.shell(
                    session_id,
                    str(item.get("command") or ""),
                    job_id=str(item.get("job_id") or self.ids.next("J")),
                    wait_ms=wait_ms,
                    max_bytes=max_bytes,
                    allow_mutation=item.get("allow_mutation", False),
                )
            elif kind == "ubuntu_batch":
                commands = item.get("commands")
                if not isinstance(commands, list):
                    raise ValueError("UBUNTU_BATCH_REQUIRES_1_TO_32_COMMANDS")
                data = self.ubuntu.batch(
                    session_id,
                    commands,
                    continue_on_error=item.get("continue_on_error", False),
                    timeout=max(0.5, min(120.0, float(item.get("timeout") or 15.0))),
                    max_bytes=max_bytes,
                )
            elif kind == "ubuntu_job_status":
                data = self.ubuntu.job_status(session_id, str(item.get("job_id") or ""))
            elif kind == "ubuntu_job_logs":
                data = self.ubuntu.job_logs(
                    session_id,
                    str(item.get("job_id") or ""),
                    stream=str(item.get("stream") or "stdout"),
                    cursor=max(0, int(item.get("cursor") or 0)),
                    max_bytes=max_bytes,
                )
            elif kind == "ubuntu_job_cancel":
                data = self.ubuntu.job_cancel(session_id, str(item.get("job_id") or ""))
            elif kind == "k8s_context":
                data = self.ubuntu.context(session_id)
            elif kind == "k8s_get":
                resource = str(item.get("resource") or "").strip()
                if not resource:
                    raise ValueError("K8S_RESOURCE_REQUIRED")
                args = ["get", resource]
                name = str(item.get("name") or "").strip()
                if name:
                    args.append(name)
                selector = str(item.get("selector") or "").strip()
                if selector:
                    args.extend(["--selector", selector])
                field_selector = str(item.get("field_selector") or "").strip()
                if field_selector:
                    args.extend(["--field-selector", field_selector])
                output = str(item.get("output") or "wide").strip()
                if output:
                    args.extend(["-o", output])
                data = self.ubuntu.kubectl(
                    session_id, args,
                    job_id=str(item.get("job_id") or self.ids.next("J")),
                    namespace=str(item.get("namespace") or ""),
                    allow_mutation=False, wait_ms=wait_ms, max_bytes=max_bytes,
                )
            elif kind == "k8s_describe":
                resource = str(item.get("resource") or "").strip()
                if not resource:
                    raise ValueError("K8S_RESOURCE_REQUIRED")
                args = ["describe", resource]
                name = str(item.get("name") or "").strip()
                if name:
                    args.append(name)
                data = self.ubuntu.kubectl(
                    session_id, args,
                    job_id=str(item.get("job_id") or self.ids.next("J")),
                    namespace=str(item.get("namespace") or ""),
                    allow_mutation=False, wait_ms=wait_ms, max_bytes=max_bytes,
                )
            elif kind == "k8s_logs":
                target = str(item.get("target") or "").strip()
                if not target:
                    raise ValueError("K8S_LOG_TARGET_REQUIRED")
                args = ["logs", target]
                container = str(item.get("container") or "").strip()
                if container:
                    args.extend(["--container", container])
                tail = int(item.get("tail") if item.get("tail") is not None else 200)
                if tail >= 0:
                    args.append(f"--tail={min(100000, tail)}")
                since = str(item.get("since") or "").strip()
                if since:
                    args.extend(["--since", since])
                if bool(item.get("timestamps", True)):
                    args.append("--timestamps")
                if bool(item.get("previous", False)):
                    args.append("--previous")
                data = self.ubuntu.kubectl(
                    session_id, args,
                    job_id=str(item.get("job_id") or self.ids.next("J")),
                    namespace=str(item.get("namespace") or ""),
                    allow_mutation=False, wait_ms=wait_ms, max_bytes=max_bytes,
                )
            elif kind == "k8s_events":
                args = ["get", "events", "--sort-by=.lastTimestamp"]
                field_selector = str(item.get("field_selector") or "").strip()
                if field_selector:
                    args.extend(["--field-selector", field_selector])
                data = self.ubuntu.kubectl(
                    session_id, args,
                    job_id=str(item.get("job_id") or self.ids.next("J")),
                    namespace=str(item.get("namespace") or ""),
                    allow_mutation=False, wait_ms=wait_ms, max_bytes=max_bytes,
                )
            elif kind == "k8s_command":
                args = item.get("args")
                if not isinstance(args, list):
                    raise ValueError("KUBECTL_ARGS_REQUIRES_1_TO_64_STRINGS")
                data = self.ubuntu.kubectl(
                    session_id,
                    [str(value) for value in args],
                    job_id=str(item.get("job_id") or self.ids.next("J")),
                    namespace=str(item.get("namespace") or ""),
                    allow_mutation=item.get("allow_mutation", False),
                    confirm_context=str(item.get("confirm_context") or ""),
                    wait_ms=wait_ms,
                    max_bytes=max_bytes,
                )
            else:
                raise ValueError(f"UNSUPPORTED_UBUNTU_OPERATION:{kind}")

            data = self._scrub_browser_secret_values(data, secret_sources)
            if isinstance(data, dict):
                data = {
                    **data,
                    "secret_refs_used": used_secret_refs,
                    "secret_values_returned": False,
                    "execution_target": "REMOTE_PC_WSL",
                    "control_plane": "EASYCHANGE_STRUCTURED",
                    "result_transport": data.get("result_transport") or "EC2_RESULT_STORE_CHUNKS",
                }
            terminal = bool(data.get("terminal")) if isinstance(data, dict) else False
            return_code = data.get("return_code") if isinstance(data, dict) else None
            ok = not terminal or return_code in (None, 0)
            return Result(
                ok,
                kind,
                data=data,
                error=None if ok else str((data or {}).get("stderr") or "REMOTE_COMMAND_FAILED")[:1000],
                code=None if ok else "REMOTE_COMMAND_FAILED",
                command_id=self.ids.next("C"),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            error = self._scrub_browser_secret_values(str(exc), secret_sources)
            return Result(
                False,
                kind or "ubuntu",
                error=error,
                code=str(getattr(exc, "code", "") or type(exc).__name__),
                data={
                    "secret_refs_used": used_secret_refs,
                    "secret_values_returned": False,
                    "execution_target": "REMOTE_PC_WSL",
                    "control_plane": "EASYCHANGE_STRUCTURED",
                },
                command_id=self.ids.next("C"),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

    def _execute_browser_operation(self, item: dict) -> Result:
            kind = str(item.get("type") or "").casefold()
            secret_sources: dict[str, str] = {}
            used_secret_refs: list[str] = []
            try:
                item, used_secret_refs, secret_sources = self._resolve_browser_secret_refs(item)
                kind = str(item.get("type") or "").casefold()
                session_id = str(item.get("session_id") or "default")
                timeout = max(0.1, min(120.0, float(item.get("timeout") or 10.0)))

                if kind == "browser_open":
                    data = self.browser.start(
                        session_id,
                        url=str(item.get("url") or "about:blank"),
                        headless=False,
                        visible=bool(item.get("visible", True)),
                        timeout=min(15.0, timeout),
                        browser_path=str(item.get("browser_path") or ""),
                    )
                elif kind == "browser_attach":
                    data = self.browser.attach(
                        session_id,
                        port=max(0, int(item.get("port") or 0)),
                        target_id=str(item.get("target_id") or ""),
                        url_contains=str(item.get("url_contains") or ""),
                        timeout=min(15.0, timeout),
                    )
                elif kind == "browser_detach":
                    data = self.browser.stop(session_id)
                elif kind == "browser_state":
                    data = self.browser.state(session_id)
                elif kind == "browser_snapshot":
                    data = self.browser.snapshot(
                        session_id,
                        max_elements=max(1, min(200, int(item.get("max_elements") or 80))),
                        max_text_blocks=max(0, min(100, int(item.get("max_text_blocks") or 40))),
                    )
                elif kind == "browser_dom":
                    data = self.browser.dom(
                        session_id,
                        cursor=max(0, int(item.get("cursor") or 0)),
                        max_bytes=max(1024, min(1024 * 1024, int(item.get("max_bytes") or 65536))),
                    )
                elif kind == "browser_visual":
                    data = self.browser.visual(
                        session_id,
                        quality=max(20, min(90, int(item.get("quality") or 55))),
                        full_page=bool(item.get("full_page", False)),
                        max_bytes=max(32768, min(2 * 1024 * 1024, int(item.get("max_bytes") or 262144))),
                    )
                elif kind == "browser_actions":
                    actions = item.get("actions")
                    if not isinstance(actions, list):
                        raise ValueError("BROWSER_ACTIONS_REQUIRED")
                    data = self.browser.execute_actions(
                        session_id,
                        actions,
                        continue_on_error=bool(item.get("continue_on_error", False)),
                        default_timeout=timeout,
                    )
                elif kind == "browser_tabs":
                    data = self.browser.tabs(session_id)
                elif kind == "browser_show":
                    data = self.browser.show(session_id)
                elif kind == "browser_hide":
                    data = self.browser.hide(session_id)
                elif kind == "browser_navigate":
                    data = self.browser.navigate(
                        session_id, str(item.get("url") or ""), timeout
                    )
                elif kind == "browser_evaluate":
                    data = self.browser.evaluate(
                        session_id, str(item.get("script") or ""), timeout
                    )
                elif kind == "browser_cdp_call":
                    data = self.browser.cdp_call(
                        session_id,
                        str(item.get("method") or ""),
                        item.get("params") if isinstance(item.get("params"), dict) else {},
                        channel=str(item.get("channel") or "page"),
                        timeout=timeout,
                        include_events=bool(item.get("include_events", True)),
                    )
                elif kind == "browser_cdp_batch":
                    calls = item.get("calls")
                    if not isinstance(calls, list):
                        raise ValueError("CDP_BATCH_CALLS_REQUIRED")
                    data = self.browser.cdp_batch(
                        session_id,
                        calls,
                        continue_on_error=bool(item.get("continue_on_error", False)),
                        timeout=timeout,
                    )
                elif kind == "browser_cdp_events":
                    data = self.browser.cdp_events(
                        session_id,
                        channel=str(item.get("channel") or "page"),
                        limit=max(0, min(512, int(item.get("limit") or 100))),
                        pump=bool(item.get("pump", True)),
                    )
                elif kind == "swagger_browser_operations":
                    data = self.browser.swagger_operations(
                        session_id,
                        limit=max(1, min(2000, int(item.get("limit") or 500))),
                    )
                elif kind == "swagger_browser_execute":
                    data = self.browser.swagger_execute(
                        session_id,
                        method=str(item.get("method") or ""),
                        path=str(item.get("path") or ""),
                        parameters=item.get("parameters") if isinstance(item.get("parameters"), dict) else {},
                        body=item.get("body"),
                        timeout=timeout,
                    )
                else:
                    raise ValueError(f"UNSUPPORTED_BROWSER_OPERATION:{kind}")

                focus = self.browser.restore_easychange_foreground(
                    attempts=10 if kind == "browser_open" else 6,
                    stable_checks=5 if kind == "browser_open" else 2,
                    stable_interval=0.08 if kind == "browser_open" else 0.04,
                    respect_user_state=True,
                    require_browser_foreground=True,
                )
                if (
                    kind == "browser_open"
                    and focus.get("focus_required")
                    and not focus.get("command_focus_confirmed")
                    and not focus.get("user_focus_override")
                ):
                    # A newly launched Edge/Chrome can steal foreground again
                    # while its first window finishes activating. Wait once,
                    # then perform one final bounded restore pass.
                    time.sleep(0.45)
                    focus = self.browser.restore_easychange_foreground(
                        attempts=10,
                        stable_checks=5,
                        stable_interval=0.08,
                        respect_user_state=True,
                        require_browser_foreground=True,
                    )
                    focus["focus_restore_second_pass"] = True
                data = self._scrub_browser_secret_values(data, secret_sources)
                if isinstance(data, dict):
                    data = {
                        **data,
                        "easychange_focus": focus,
                        "command_focus_confirmed": bool(focus.get("command_focus_confirmed")),
                        "secret_refs_used": used_secret_refs,
                        "secret_values_returned": False,
                    }
                if (
                    focus.get("focus_required")
                    and not focus.get("command_focus_confirmed")
                    and not focus.get("user_focus_override")
                ):
                    return Result(
                        False,
                        kind,
                        error="EASYCHANGE_FOREGROUND_RESTORE_FAILED",
                        code="FOCUS_RECOVERY_FAILED",
                        data=data,
                        command_id=self.ids.next("C"),
                    )
                return Result(
                    True,
                    kind,
                    data=data,
                    command_id=self.ids.next("C"),
                )
            except Exception as exc:
                focus = self.browser.restore_easychange_foreground(
                    attempts=10 if kind == "browser_open" else 6,
                    stable_checks=5 if kind == "browser_open" else 2,
                    stable_interval=0.08 if kind == "browser_open" else 0.04,
                    respect_user_state=True,
                    require_browser_foreground=True,
                )
                error = self._scrub_browser_secret_values(str(exc), secret_sources)
                diagnostics = getattr(exc, "diagnostics", {})
                diagnostics = self._scrub_browser_secret_values(
                    diagnostics if isinstance(diagnostics, dict) else {},
                    secret_sources,
                )
                return Result(
                    False,
                    kind or "browser",
                    error=error,
                    code=str(getattr(exc, "code", "") or type(exc).__name__),
                    data={
                        "easychange_focus": focus,
                        "command_focus_confirmed": bool(focus.get("command_focus_confirmed")),
                        "bootstrap": diagnostics,
                        "secret_refs_used": used_secret_refs,
                        "secret_values_returned": False,
                    },
                    command_id=self.ids.next("C"),
                )
    def _execute_structured(
        self,
        payload: dict,
        *,
        persist: bool = True,
        sequence: str | None = None,
        external_env: dict[str, str] | None = None,
    ) -> Result:
        """Execute bounded HID-delivered operations; read-only batches avoid transactions."""
        started = time.monotonic()
        operation = str(payload.get("op") or "").casefold()
        operations = payload.get("operations") if operation == "batch" else [payload]
        if not isinstance(operations, list) or not 1 <= len(operations) <= 64:
            return Result(False, "structured", error="OPERATION_COUNT_LIMIT", code="INVALID_STRUCTURED_PAYLOAD")
        continue_on_error = payload.get("continue_on_error", False)
        if type(continue_on_error) is not bool:
            return Result(False, "structured",
                          error="CONTINUE_ON_ERROR_BOOLEAN_REQUIRED",
                          code="INVALID_STRUCTURED_PAYLOAD")

        # Transport timing is a tiny control-plane operation. It bypasses
        # project/index work just like durable result recovery and is persisted
        # only in the workspace's EasyChange session state.
        if len(operations) == 1 and isinstance(operations[0], dict):
            query_item = operations[0]
            query_kind = str(query_item.get("type") or "").casefold()
            if query_kind in {"optical_transport_get", "optical_transport_configure"}:
                try:
                    if query_kind == "optical_transport_configure":
                        data = self.configure_optical_transport(
                            adaptive_enabled=query_item.get("adaptive_enabled"),
                            min_ms=query_item.get("min_ms"),
                            max_ms=query_item.get("max_ms"),
                            current_ms=query_item.get("current_ms"),
                            step_ms=query_item.get("step_ms"),
                            adjusted_by=str(query_item.get("adjusted_by") or "mcp"),
                        )
                    else:
                        data = self.optical_transport_config()
                except (TypeError, ValueError) as exc:
                    return Result(
                        False,
                        query_kind,
                        error=str(exc),
                        code="INVALID_OPTICAL_TRANSPORT_CONFIG",
                    )
                return Result(
                    True,
                    query_kind,
                    data=data,
                    command_id=self.ids.next("C"),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

            # Small durable-result queries intentionally bypass project/index work.
            # They are the recovery API used when HDMI lost the original frame.
            if query_kind in STRUCTURED_RECOVERY_TYPES:
                target_sequence = str(query_item.get("sequence") or query_item.get("target_sequence") or "").upper()
                if not re.fullmatch(r"Q[A-Z0-9_-]{4,40}", target_sequence):
                    return Result(False, query_kind, error="VALID_TARGET_SEQUENCE_REQUIRED", code="INVALID_SEQUENCE")
                # Validate chunk sizing on the small control-plane request
                # before touching stored multi-megabyte result payloads.
                sized_kinds={"result_header","result_meta","result_chunk",
                             "result_chunks","result_stream_meta",
                             "result_stream_chunk","result_stream_chunks"}
                parsed_chunk_bytes=None
                if query_kind in sized_kinds:
                    raw_size=query_item.get("chunk_bytes")
                    if raw_size not in (None,"",0):
                        if type(raw_size) is int:
                            parsed_chunk_bytes=raw_size
                        elif (isinstance(raw_size,str) and len(raw_size)<=32
                              and raw_size.strip().lstrip("+-").isdecimal()):
                            parsed_chunk_bytes=int(raw_size)
                        else:
                            return Result(
                                False,query_kind,error="INVALID_RESULT_CHUNK_BYTES",
                                code=("INVALID_RESULT_STREAM_CHUNK"
                                      if "stream" in query_kind
                                      else "INVALID_RESULT_CHUNK"))
                        parsed_chunk_bytes=max(256,min(4096,parsed_chunk_bytes))
                if query_kind in {"result_chunk", "result_stream_chunk"}:
                    raw_index = query_item.get("chunk_index")
                    try:
                        if type(raw_index) is int:
                            if raw_index < 0:
                                raise ValueError("INVALID_RESULT_CHUNK_INDEX")
                            parsed_index = raw_index
                        elif (isinstance(raw_index, str) and len(raw_index) <= 32
                              and raw_index.strip().isdecimal()):
                            parsed_index = int(raw_index)
                        else:
                            raise ValueError("INVALID_RESULT_CHUNK_INDEX")
                    except (ValueError, OverflowError) as exc:
                        return Result(
                            False, query_kind, error=str(exc),
                            code=("INVALID_RESULT_STREAM_CHUNK"
                                  if query_kind == "result_stream_chunk"
                                  else "INVALID_RESULT_CHUNK"),
                        )
                else:
                    parsed_index = None
                if query_kind in {"result_stream_meta", "result_stream_chunk",
                                  "result_stream_chunks"}:
                    raw_ratio = query_item.get("max_ratio")
                    try:
                        if (isinstance(raw_ratio, bool) or
                                (isinstance(raw_ratio, str) and len(raw_ratio) > 32)):
                            raise ValueError("INVALID_RESULT_STREAM_MAX_RATIO")
                        parsed_ratio = float(raw_ratio or 0.85)
                        if not math.isfinite(parsed_ratio):
                            raise ValueError("INVALID_RESULT_STREAM_MAX_RATIO")
                    except (ValueError, TypeError, OverflowError):
                        return Result(
                            False, query_kind, error="INVALID_RESULT_STREAM_MAX_RATIO",
                            code="INVALID_RESULT_STREAM_CHUNK",
                        )
                else:
                    parsed_ratio = None
                if query_kind in {"result_status", "mutation_status"}:
                    data = self.result_store.status(target_sequence)
                elif query_kind == "result_header":
                    data = self.result_store.header(
                        target_sequence,
                        chunk_bytes=(parsed_chunk_bytes if parsed_chunk_bytes is not None else 640),
                    )
                elif query_kind == "result_get":
                    data = self.result_store.result(target_sequence)
                elif query_kind == "result_meta":
                    data = self.result_store.chunk_meta(target_sequence, chunk_bytes=(parsed_chunk_bytes if parsed_chunk_bytes is not None else 320))
                elif query_kind == "result_chunk":
                    try:
                        data = self.result_store.chunk(
                            target_sequence,
                            parsed_index,
                            chunk_bytes=(parsed_chunk_bytes if parsed_chunk_bytes is not None else 320),
                            compress_payload=bool(query_item.get("compressed")),
                        )
                    except (TypeError, ValueError, IndexError) as exc:
                        return Result(False, query_kind, error=str(exc), code="INVALID_RESULT_CHUNK")
                elif query_kind == "result_chunks":
                    try:
                        raw_indexes = query_item.get("chunk_indexes")
                        if not isinstance(raw_indexes, list) or not 1 <= len(raw_indexes) <= 8:
                            raise ValueError("RESULT_CHUNK_INDEXES_REQUIRES_1_TO_8_ITEMS")
                        data = self.result_store.chunks(
                            target_sequence,
                            raw_indexes,
                            chunk_bytes=(parsed_chunk_bytes if parsed_chunk_bytes is not None else 1536),
                            max_chunks=8,
                            compress_payload=bool(query_item.get("compressed")),
                        )
                    except (TypeError, ValueError, IndexError) as exc:
                        return Result(False, query_kind, error=str(exc), code="INVALID_RESULT_CHUNK")
                elif query_kind == "result_stream_meta":
                    data = self.result_store.stream_meta(
                        target_sequence,
                        chunk_bytes=(parsed_chunk_bytes if parsed_chunk_bytes is not None else 2048),
                        max_ratio=parsed_ratio,
                    )
                elif query_kind == "result_stream_chunk":
                    try:
                        data = self.result_store.stream_chunk(
                            target_sequence,
                            parsed_index,
                            chunk_bytes=(parsed_chunk_bytes if parsed_chunk_bytes is not None else 2048),
                            max_ratio=parsed_ratio,
                        )
                    except (TypeError, ValueError, IndexError) as exc:
                        return Result(False, query_kind, error=str(exc), code="INVALID_RESULT_STREAM_CHUNK")
                elif query_kind == "result_stream_chunks":
                    try:
                        raw_indexes = query_item.get("chunk_indexes")
                        if not isinstance(raw_indexes, list) or not 1 <= len(raw_indexes) <= 8:
                            raise ValueError("RESULT_STREAM_CHUNK_INDEXES_REQUIRES_1_TO_8_ITEMS")
                        data = self.result_store.stream_chunks(
                            target_sequence,
                            raw_indexes,
                            chunk_bytes=(parsed_chunk_bytes if parsed_chunk_bytes is not None else 2048),
                            max_chunks=8,
                            max_ratio=parsed_ratio,
                        )
                    except (TypeError, ValueError, IndexError) as exc:
                        return Result(False, query_kind, error=str(exc), code="INVALID_RESULT_STREAM_CHUNK")
                else:
                    # Optical recovery must reject bogus frame indexes before
                    # loading a multi-megabyte durable result or encoding EC2.
                    if query_kind in {"optical_chunk", "optical_chunks"}:
                        raw_indexes = (query_item.get("chunk_indexes")
                                       if query_kind == "optical_chunks"
                                       else [query_item.get("chunk_index")])
                        upper = 16 if query_kind == "optical_chunks" else 1
                        if not isinstance(raw_indexes,list) or not 1<=len(raw_indexes)<=upper:
                            return Result(False,query_kind,
                                          error="INVALID_CHUNK_INDEX",
                                          code="INVALID_RESULT_CHUNK")
                        if any(
                            type(item) is not int and not (
                                isinstance(item,str) and len(item)<=32
                                and item.strip().isdecimal()
                            )
                            for item in raw_indexes
                        ):
                            return Result(False,query_kind,
                                          error="INVALID_CHUNK_INDEX",
                                          code="INVALID_RESULT_CHUNK")
                    original_result = self.result_store.result(target_sequence)
                    if original_result is None:
                        data = None
                    else:
                        from ..remote.optical_protocol import encode_result_chunks
                        if query_kind == "optical_meta":
                            first = json.loads(
                                encode_result_chunks(original_result, max_packets=1)[0]
                            )
                            data = {
                                "target_sequence": target_sequence,
                                "total_chunks": int(first.get("n") or 0),
                                "result_hash": first.get("h"),
                                "crc32": first.get("c"),
                            }
                        elif query_kind == "optical_chunks":
                            try:
                                raw_indexes = query_item.get("chunk_indexes")
                                if not isinstance(raw_indexes, list) or not 1 <= len(raw_indexes) <= 16:
                                    raise ValueError("CHUNK_INDEXES_REQUIRES_1_TO_16_ITEMS")
                                indexes = []
                                for raw_index in raw_indexes:
                                    if (type(raw_index) is not int and
                                            not (isinstance(raw_index, str)
                                                 and raw_index.strip().isdecimal())):
                                        raise ValueError("INVALID_CHUNK_INDEX")
                                    index = int(raw_index)
                                    if index < 0:
                                        raise IndexError("OPTICAL_CHUNK_OUT_OF_RANGE")
                                    if index not in indexes:
                                        indexes.append(index)
                                selected_packets = [
                                    json.loads(packet)
                                    for packet in encode_result_chunks(
                                        original_result, indexes=indexes
                                    )
                                ]
                                data = {
                                    "target_sequence": target_sequence,
                                    "requested": indexes,
                                    "total_chunks": int(selected_packets[0].get("n") or 0),
                                    "packets": selected_packets,
                                    "chunk_crc32": {
                                        str(packet["i"]): f"{binascii.crc32(str(packet.get('d') or '').encode('ascii')) & 0xffffffff:08x}"
                                        for packet in selected_packets
                                    },
                                    "complete": len(selected_packets) == len(indexes),
                                }
                            except (TypeError, ValueError, IndexError) as exc:
                                return Result(False, query_kind, error=str(exc), code="INVALID_RESULT_CHUNK")
                        else:
                            try:
                                raw_index = query_item.get("chunk_index")
                                if (type(raw_index) is not int and
                                        not (isinstance(raw_index, str)
                                             and raw_index.strip().isdecimal())):
                                    raise ValueError("INVALID_CHUNK_INDEX")
                                chunk_index = int(raw_index)
                                packet = json.loads(
                                    encode_result_chunks(
                                        original_result, indexes=[chunk_index]
                                    )[0]
                                )
                                data = {
                                    "target_sequence": target_sequence,
                                    "chunk_index": chunk_index,
                                    "packet": packet,
                                }
                            except (TypeError, ValueError, IndexError) as exc:
                                return Result(False, query_kind, error=str(exc), code="INVALID_RESULT_CHUNK")
                if data is None:
                    return Result(False, query_kind, error="RESULT_NOT_FOUND", code="RESULT_NOT_FOUND")
                return Result(True, query_kind, data=data, command_id=self.ids.next("C"),
                              duration_ms=int((time.monotonic() - started) * 1000))

        # Ubuntu/WSL/Kubernetes operations execute inside EasyChange on the
        # REMOTE_PC. Results use the same sequenced durable EC2/result-store
        # contract as browser operations, but never depend on terminal focus,
        # OCR, clipboard or a visible shell window.
        if len(operations) == 1 and isinstance(operations[0], dict):
            ubuntu_item = operations[0]
            ubuntu_kind = str(ubuntu_item.get("type") or "").casefold()
            if ubuntu_kind in {
                "ubuntu_open", "ubuntu_state", "ubuntu_close", "ubuntu_cd", "ubuntu_env",
                "ubuntu_exec", "ubuntu_shell", "ubuntu_batch",
                "ubuntu_job_status", "ubuntu_job_logs", "ubuntu_job_cancel",
                "k8s_context", "k8s_get", "k8s_describe", "k8s_logs", "k8s_events", "k8s_command",
            }:
                return self._execute_ubuntu_operation(ubuntu_item)

        # Browser operations use the same sequenced structured/result-store
        # contract as all other EasyChange tools. BrowserService reuses an existing
        # Edge/Chrome session first and may open an already-installed Edge/Chrome
        # without installing software or creating a disposable browser profile.
        if len(operations) == 1 and isinstance(operations[0], dict):
            browser_item = operations[0]
            browser_kind = str(browser_item.get("type") or "").casefold()
            if browser_kind in {
                "browser_open", "browser_attach", "browser_detach", "browser_state",
                "browser_snapshot", "browser_dom", "browser_visual",
                "browser_actions", "browser_tabs", "browser_show", "browser_hide",
                "browser_navigate", "browser_evaluate",
                "browser_cdp_call", "browser_cdp_batch", "browser_cdp_events",
                "swagger_browser_operations", "swagger_browser_execute",
            }:
                return self._execute_browser_operation(browser_item)

        # Structured argv execution always uses the durable job executor. Fast
        # commands can finish inside wait_ms; long commands return a small job
        # receipt and continue without holding the optical session.
        if len(operations) == 1 and isinstance(operations[0], dict):
            process_item = operations[0]
            process_kind = str(process_item.get("type") or "").casefold()
            if process_kind == "process_exec":
                try:
                    argv = process_item.get("argv")
                    if not isinstance(argv, list) or not 1 <= len(argv) <= 64 or not all(
                        isinstance(value, str) and value and len(value) <= 4096
                        for value in argv
                    ):
                        return Result(
                            False, process_kind,
                            error="ARGV_REQUIRES_1_TO_64_STRINGS",
                            code="INVALID_STRUCTURED_PAYLOAD",
                        )
                    job_id = str(process_item.get("job_id") or "") or self.ids.next("J")
                    wait_ms = max(0, min(10000, int(process_item.get("wait_ms") or 1200)))
                    started_job = self.processes.job_start(argv, job_id)
                    deadline = time.monotonic() + (wait_ms / 1000.0)
                    status = self.processes.job_status(job_id)
                    while (
                        not bool(status.get("terminal"))
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.02)
                        status = self.processes.job_status(job_id)

                    terminal = bool(status.get("terminal"))
                    result_data = (
                        self.processes.job_result(job_id)
                        if terminal
                        else status
                    )
                    state = str(result_data.get("state") or "RUNNING").upper()
                    return_code = result_data.get("returncode")
                    success = (
                        return_code == 0 and state == "SUCCEEDED"
                        if terminal
                        else None
                    )
                    stdout_bytes = int(result_data.get("stdout_bytes") or result_data.get("stdout_chars") or 0)
                    stderr_bytes = int(result_data.get("stderr_bytes") or result_data.get("stderr_chars") or 0)
                    data = {
                        "job_id": job_id,
                        "pid": result_data.get("pid") or started_job.get("pid"),
                        "argv": list(argv),
                        "exec_argv": result_data.get("exec_argv") or started_job.get("exec_argv"),
                        "transport": {
                            "execution_channel": "healthy",
                            "delivery_channel": "not_started",
                        },
                        "execution": {
                            "state": "FINISHED" if terminal else "RUNNING",
                            "process_state": state,
                            "success": success,
                            "return_code": return_code,
                            "started_at": result_data.get("started_at") or started_job.get("started_at"),
                            "finished_at": result_data.get("finished_at"),
                            "duration_ms": result_data.get("duration_ms"),
                        },
                        "result": {
                            "available": terminal,
                            "bytes": stdout_bytes + stderr_bytes,
                            "stdout_bytes": stdout_bytes,
                            "stderr_bytes": stderr_bytes,
                            "truncated": bool(result_data.get("output_truncated")),
                            "cursor": 0 if (stdout_bytes or stderr_bytes) else None,
                            "paged_logs": True,
                        },
                        "mutation": {"state": "NOT_APPLICABLE"},
                        "stdout": result_data.get("stdout", "") if terminal else "",
                        "stderr": result_data.get("stderr", "") if terminal else "",
                    }
                    return Result(
                        True,
                        process_kind,
                        data=data,
                        command_id=self.ids.next("C"),
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                except Exception as exc:
                    return Result(
                        False,
                        process_kind,
                        error=str(exc),
                        code=str(getattr(exc, "code", "") or type(exc).__name__),
                        command_id=self.ids.next("C"),
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )

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
                    elif job_kind == "job_logs":
                        if not job_id:
                            return Result(False, job_kind, error="JOB_ID_REQUIRED", code="INVALID_JOB_ID")
                        data = self.processes.job_logs(
                            job_id,
                            stream=str(job_item.get("stream") or "stdout"),
                            cursor=max(0, int(job_item.get("cursor") or 0)),
                            max_bytes=max(256, min(1024 * 1024, int(job_item.get("max_bytes") or 16384))),
                        )
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
                        cursor = str(item.get("cursor") or "").strip()
                        if cursor:
                            args += ["--cursor", cursor]
                        result = self.execute_tokens("read-many", args, raw="EC1 read-many", persist=sub_persist)
                elif kind == "command_schema":
                    command_name = str(item.get("command") or "").strip()
                    if not command_name:
                        result = Result(False, kind, error="COMMAND_REQUIRED",
                                        code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        try:
                            result = Result(True, kind, data=command_schema(command_name))
                        except LookupError as exc:
                            result = Result(False, kind, error=str(exc), code="UNKNOWN_COMMAND")
                elif kind == "secret_broker_public":
                    result = Result(True, kind, data=self.secret_broker.public_metadata())
                elif kind == "secret_broker_status":
                    result = Result(True, kind, data=self.secret_broker.status())
                elif kind == "secret_broker_import":
                    required = {
                        "broker_id": item.get("broker_id"),
                        "source_name": item.get("source_name"),
                        "ephemeral_public_b64": item.get("ephemeral_public_b64"),
                        "nonce_b64": item.get("nonce_b64"),
                        "ciphertext_b64": item.get("ciphertext_b64"),
                    }
                    if not all(isinstance(value, str) and value for value in required.values()):
                        result = Result(
                            False,
                            kind,
                            error="SECRET_ENVELOPE_FIELDS_REQUIRED",
                            code="INVALID_STRUCTURED_PAYLOAD",
                        )
                    else:
                        result = Result(
                            True,
                            kind,
                            data=self.secret_broker.import_envelope(**required),
                        )
                elif kind == "secret_broker_import_source":
                    source_name = str(item.get("source_name") or "").strip()
                    source_path = str(item.get("path") or "").strip()
                    if not source_name or not source_path:
                        result = Result(
                            False,
                            kind,
                            error="SECRET_SOURCE_NAME_AND_PATH_REQUIRED",
                            code="INVALID_STRUCTURED_PAYLOAD",
                        )
                    else:
                        try:
                            secret, metadata = self._secret_value_from_workspace_source(
                                source_path,
                                source_format=str(item.get("source_format") or "raw"),
                                key=str(item.get("key") or ""),
                            )
                            imported = self.secret_broker.import_local_value(
                                source_name=source_name,
                                value=secret,
                            )
                            result = Result(
                                True,
                                kind,
                                data={**imported, **metadata},
                            )
                        except Exception as exc:
                            result = Result(
                                False,
                                kind,
                                error=str(exc),
                                code=str(getattr(exc, "code", "") or type(exc).__name__),
                                data={"secret_values_returned": False},
                            )
                elif kind == "human_text_info":
                    result = Result(True, kind, data=self.human_text.info())
                elif kind == "human_text_read":
                    result = Result(True, kind, data=self.human_text.read(
                        cursor=max(0, int(item.get("cursor") or 0)),
                        max_bytes=max(
                            512,
                            min(256 * 1024, int(item.get("max_bytes") or 16384)),
                        ),
                    ))
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
                        str(item.get("profile_id") or ""),
                        kind=str(item.get("kind") or "custom"),
                        run_argv=item.get("run_argv"),
                        test_argv=item.get("test_argv"),
                        build_argv=item.get("build_argv"),
                        urls=item.get("urls") if isinstance(item.get("urls"), list) else None,
                        env=item.get("env") if isinstance(item.get("env"), dict) else None,
                        env_refs=item.get("env_refs") if isinstance(item.get("env_refs"), dict) else None,
                        inherit_env=bool(item.get("inherit_env", True)),
                        env_allowlist=(
                            item.get("env_allowlist")
                            if isinstance(item.get("env_allowlist"), list)
                            else None
                        ),
                    ))
                elif kind == "runtime_environment":
                    result = Result(
                        True,
                        kind,
                        data=self.runtime.environment_status(str(item.get("profile") or "")),
                    )
                elif kind == "runtime_environment_status":
                    result = Result(
                        True,
                        kind,
                        data=self.runtime.environment_resolution_status(
                            str(item.get("profile") or ""),
                            env_key=str(item.get("env_key") or ""),
                            ref_name=str(item.get("ref") or item.get("ref_name") or ""),
                        ),
                    )
                elif kind == "git_diff":
                    result = Result(
                        True,
                        kind,
                        data=self.git.diff_refs(
                            base_ref=str(item.get("base_ref") or ""),
                            head_ref=str(item.get("head_ref") or "HEAD"),
                            path=str(item.get("path") or "") or None,
                            name_only=bool(item.get("name_only", False)),
                            name_status=bool(item.get("name_status", False)),
                            unified_lines=max(0, min(100, int(item.get("unified_lines") or 3))),
                            cursor=max(0, int(item.get("cursor") or 0)),
                            max_bytes=max(256, min(2 * 1024 * 1024, int(item.get("max_bytes") or 65536))),
                            max_items=max(1, min(5000, int(item.get("max_items") or 500))),
                        ),
                    )
                elif kind == "test_job_start":
                    job_id = str(item.get("job_id") or self.ids.next("J"))
                    result = Result(True, kind, data=self.runtime.start_test_job(
                        job_id,
                        str(item.get("profile") or ""),
                        external_env=external_env,
                    ))
                elif kind == "test_job_result":
                    job_id = str(item.get("job_id") or "")
                    if not job_id:
                        result = Result(False, kind, error="JOB_ID_REQUIRED", code="INVALID_JOB_ID")
                    else:
                        data = self.runtime.test_job_result(job_id)
                        result = Result(
                            data.get("passed") is not False,
                            kind,
                            data=data,
                            error="Smart test job failed" if data.get("passed") is False else None,
                            code="TEST_FAILED" if data.get("passed") is False else None,
                        )
                elif kind == "test_job_logs":
                    job_id = str(item.get("job_id") or "")
                    if not job_id:
                        result = Result(False, kind, error="JOB_ID_REQUIRED", code="INVALID_JOB_ID")
                    else:
                        result = Result(True, kind, data=self.runtime.runtime_job_logs(
                            job_id,
                            stream=str(item.get("stream") or "stdout"),
                            cursor=max(0, int(item.get("cursor") or 0)),
                            max_bytes=max(256, min(1024 * 1024, int(item.get("max_bytes") or 16384))),
                        ))
                elif kind == "build_job_start":
                    job_id = str(item.get("job_id") or self.ids.next("J"))
                    result = Result(True, kind, data=self.runtime.start_build_job(
                        job_id,
                        str(item.get("profile") or ""),
                        external_env=external_env,
                    ))
                elif kind == "build_job_result":
                    job_id = str(item.get("job_id") or "")
                    if not job_id:
                        result = Result(False, kind, error="JOB_ID_REQUIRED", code="INVALID_JOB_ID")
                    else:
                        data = self.runtime.build_job_result(
                            job_id,
                            retry_owned_lock=bool(item.get("retry_owned_lock", False)),
                        )
                        retry_started = bool(data.get("retry_started"))
                        result = Result(
                            data.get("passed") is not False or retry_started,
                            kind,
                            data=data,
                            error=(
                                "Build job failed"
                                if data.get("passed") is False and not retry_started
                                else None
                            ),
                            code=(
                                "BUILD_FAILED"
                                if data.get("passed") is False and not retry_started
                                else None
                            ),
                        )
                elif kind == "build_job_logs":
                    job_id = str(item.get("job_id") or "")
                    if not job_id:
                        result = Result(False, kind, error="JOB_ID_REQUIRED", code="INVALID_JOB_ID")
                    else:
                        result = Result(True, kind, data=self.runtime.runtime_job_logs(
                            job_id,
                            stream=str(item.get("stream") or "stdout"),
                            cursor=max(0, int(item.get("cursor") or 0)),
                            max_bytes=max(256, min(1024 * 1024, int(item.get("max_bytes") or 16384))),
                        ))
                elif kind == "run_project":
                    profile = str(item.get("profile") or "")
                    process_id = self.ids.next("P")
                    result = Result(
                        True,
                        kind,
                        data=self.runtime.start(
                            process_id,
                            profile,
                            external_env=external_env,
                        ),
                    )
                elif kind == "test_smart":
                    profile = str(item.get("profile") or "")
                    timeout = max(5, min(3600, int(item.get("timeout") or 600)))
                    data = self.runtime.smart_test(
                        profile,
                        timeout,
                        external_env=external_env,
                    )
                    result = Result(bool(data.get("passed")), kind, data=data,
                                    error=None if data.get("passed") else "Smart test failed",
                                    code=None if data.get("passed") else "TEST_FAILED")
                elif kind == "test_evidence":
                    profile = str(item.get("profile") or "")
                    timeout = max(5, min(3600, int(item.get("timeout") or 600)))
                    data = self.runtime.test_with_evidence(
                        profile,
                        timeout,
                        str(item.get("title") or "test-evidence"),
                        external_env=external_env,
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
                        process_id,
                        profile,
                        url,
                        timeout,
                        bool(item.get("screenshot", True)),
                        bool(item.get("start_if_needed", True)),
                        bool(item.get("keep_running", False)),
                        external_env=external_env,
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
                elif kind == "ef_migrations_list":
                    migration_project = str(item.get("migration_project") or "")
                    if not migration_project:
                        result = Result(False, kind, error="MIGRATION_PROJECT_REQUIRED",
                                        code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        result = Result(True, kind, data=self.ef_migrations.migrations_list(
                            migration_project=migration_project,
                            startup_project=str(item.get("startup_project") or "") or None,
                            context=str(item.get("context") or "") or None,
                            environment=str(item.get("environment") or ""),
                            connection_alias=str(item.get("connection_alias") or ""),
                            profile_id=str(item.get("profile") or ""),
                            timeout=max(30, min(1800, int(item.get("timeout") or 300))),
                            external_env=external_env,
                        ))
                elif kind == "ef_script":
                    migration_project = str(item.get("migration_project") or "")
                    if not migration_project:
                        result = Result(False, kind, error="MIGRATION_PROJECT_REQUIRED",
                                        code="INVALID_STRUCTURED_PAYLOAD")
                    else:
                        result = Result(True, kind, data=self.ef_migrations.script(
                            migration_project=migration_project,
                            startup_project=str(item.get("startup_project") or "") or None,
                            context=str(item.get("context") or "") or None,
                            environment=str(item.get("environment") or ""),
                            target_migration=str(item.get("target_migration") or ""),
                            profile_id=str(item.get("profile") or ""),
                            timeout=max(30, min(1800, int(item.get("timeout") or 300))),
                            cursor=max(0, int(item.get("cursor") or 0)),
                            max_bytes=max(512, min(1024 * 1024, int(item.get("max_bytes") or 16384))),
                            external_env=external_env,
                        ))
                elif kind == "ef_database_update":
                    migration_project = str(item.get("migration_project") or "")
                    connection_alias = str(item.get("connection_alias") or "")
                    if len(operations) > 1:
                        result = Result(False, kind,
                                        error="EF_DATABASE_UPDATE_CANNOT_BE_MIXED_IN_BATCH",
                                        code="NON_ATOMIC_EXTERNAL_WRITE")
                    elif not migration_project or not connection_alias:
                        result = Result(False, kind,
                                        error="MIGRATION_PROJECT_AND_CONNECTION_ALIAS_REQUIRED",
                                        code="INVALID_STRUCTURED_PAYLOAD")
                    elif not bool(item.get("apply_authorized", False)):
                        result = Result(False, kind,
                                        error="EF_DATABASE_UPDATE_REQUIRES_EXPLICIT_AUTHORIZATION",
                                        code="EXPLICIT_AUTHORIZATION_REQUIRED")
                    else:
                        data = self.ef_migrations.database_update(
                            migration_project=migration_project,
                            startup_project=str(item.get("startup_project") or "") or None,
                            context=str(item.get("context") or "") or None,
                            environment=str(item.get("environment") or ""),
                            connection_alias=connection_alias,
                            target_migration=str(item.get("target_migration") or ""),
                            profile_id=str(item.get("profile") or ""),
                            apply_authorized=bool(item.get("apply_authorized", False)),
                            timeout=max(30, min(3600, int(item.get("timeout") or 600))),
                            external_env=external_env,
                        )
                        result = Result(True, kind, data=data)
                elif kind == "db_query":
                    connection = str(item.get("connection") or "")
                    sql = str(item.get("sql") or "")
                    allow_write = item.get("allow_write", False)
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

        single_failure = (
            results[0]
            if not ok and not transactional and len(results) == 1 and isinstance(results[0], dict)
            else None
        )
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
            error=(
                None
                if ok
                else (
                    "Structured batch failed and was rolled back"
                    if transactional
                    else str((single_failure or {}).get("error") or "Structured read batch failed")
                )
            ),
            code=(
                None
                if ok
                else str((single_failure or {}).get("code") or "STRUCTURED_BATCH_FAILED")
            ),
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

    def _secret_value_from_workspace_source(
        self,
        path: str,
        *,
        source_format: str = "raw",
        key: str = "",
    ) -> tuple[str, dict]:
        """Read one secret locally without ever placing plaintext in a Result."""
        source_path = str(path or "").strip()
        if not source_path:
            raise ValueError("SECRET_SOURCE_PATH_REQUIRED")
        resolved = self.workspace.resolve(source_path, must_exist=True)
        if not resolved.is_file():
            raise ValueError("SECRET_SOURCE_MUST_BE_FILE")

        relative_source = resolved.relative_to(self.workspace.root_path).as_posix()
        protected = [
            str(item)
            for item in self.state_store.data.get("sensitive_source_paths", [])
            if str(item).strip()
        ]
        if relative_source not in protected:
            protected.append(relative_source)
            self.state_store.data["sensitive_source_paths"] = protected[-128:]
            self.state_store.save()
        self.indexer.protect_path(relative_source, scrub=True)

        try:
            size = int(resolved.stat().st_size)
        except OSError:
            size = 0
        if size <= 0:
            raise ValueError("SECRET_SOURCE_EMPTY")
        if size > 64 * 1024:
            raise ValueError("SECRET_SOURCE_FILE_TOO_LARGE")
        # st_size is only a preflight: another process may grow or replace
        # the file between stat() and read(). Cap the actual read as well,
        # including secret sources that are concurrently updated.
        with resolved.open("rb") as stream:
            raw = stream.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("SECRET_SOURCE_FILE_TOO_LARGE")
        if b"\0" in raw:
            raise ValueError("SECRET_SOURCE_BINARY_FORBIDDEN")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("SECRET_SOURCE_UTF8_REQUIRED") from exc

        mode = str(source_format or "raw").strip().casefold()
        selector = str(key or "").strip()
        value: object
        if mode == "raw":
            if selector:
                raise ValueError("SECRET_SOURCE_RAW_DOES_NOT_ACCEPT_KEY")
            value = text.strip()
        elif mode in {"env", "dotenv", "kv"}:
            if not selector:
                raise ValueError("SECRET_SOURCE_KEY_REQUIRED")
            entries: dict[str, str] = {}
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.casefold().startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    continue
                name, raw_value = line.split("=", 1)
                name = name.strip()
                if not name:
                    continue
                parsed = raw_value.strip()
                if len(parsed) >= 2 and parsed[0] == parsed[-1] and parsed[0] in {'"', "'"}:
                    parsed = parsed[1:-1]
                entries[name] = parsed
            if selector in entries:
                value = entries[selector]
            else:
                match = next(
                    (candidate for name, candidate in entries.items()
                     if name.casefold() == selector.casefold()),
                    None,
                )
                if match is None:
                    raise LookupError("SECRET_SOURCE_KEY_NOT_FOUND")
                value = match
        elif mode == "json":
            if not selector:
                raise ValueError("SECRET_SOURCE_KEY_REQUIRED")
            try:
                current: object = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError("SECRET_SOURCE_JSON_INVALID") from exc
            parts = [part for part in re.split(r"[.:]", selector) if part]
            if not parts:
                raise ValueError("SECRET_SOURCE_KEY_REQUIRED")
            for part in parts:
                if isinstance(current, dict):
                    if part in current:
                        current = current[part]
                        continue
                    actual = next(
                        (name for name in current if str(name).casefold() == part.casefold()),
                        None,
                    )
                    if actual is None:
                        raise LookupError("SECRET_SOURCE_KEY_NOT_FOUND")
                    current = current[actual]
                    continue
                if isinstance(current, list) and part.isdigit():
                    index = int(part)
                    if index < 0 or index >= len(current):
                        raise LookupError("SECRET_SOURCE_KEY_NOT_FOUND")
                    current = current[index]
                    continue
                raise LookupError("SECRET_SOURCE_KEY_NOT_FOUND")
            if isinstance(current, (dict, list)) or current is None:
                raise ValueError("SECRET_SOURCE_VALUE_MUST_BE_SCALAR")
            value = current
        else:
            raise ValueError("SECRET_SOURCE_FORMAT_MUST_BE_RAW_ENV_OR_JSON")

        secret = str(value)
        if not secret:
            raise ValueError("SECRET_VALUE_EMPTY")
        relative = resolved.relative_to(self.workspace.root_path).as_posix()
        return secret, {
            "source_path": relative,
            "source_format": "env" if mode in {"env", "dotenv", "kv"} else mode,
            "selector": selector or None,
            "secret_values_returned": False,
        }


    def optical_transport_config(self) -> dict:
        """Return normalized EC2 optical timing configuration for this workspace."""
        defaults = {
            "adaptive_enabled": True,
            "min_ms": 600,
            "max_ms": 1800,
            "current_ms": 900,
            "step_ms": 100,
        }
        raw = self.state_store.data.get("optical_transport")
        if not isinstance(raw, dict):
            raw = {}
        adaptive_enabled = bool(raw.get("adaptive_enabled", defaults["adaptive_enabled"]))
        min_ms = int(raw.get("min_ms") or defaults["min_ms"])
        max_ms = int(raw.get("max_ms") or defaults["max_ms"])
        current_ms = int(raw.get("current_ms") or defaults["current_ms"])
        step_ms = int(raw.get("step_ms") or defaults["step_ms"])
        min_ms = max(300, min(5000, min_ms))
        max_ms = max(300, min(5000, max_ms))
        if min_ms > max_ms:
            min_ms, max_ms = max_ms, min_ms
        current_ms = max(min_ms, min(max_ms, current_ms))
        step_ms = max(25, min(500, step_ms))
        return {
            "adaptive_enabled": adaptive_enabled,
            "mode": "adaptive" if adaptive_enabled else "manual",
            "min_ms": min_ms,
            "max_ms": max_ms,
            "current_ms": current_ms,
            "step_ms": step_ms,
            "supported_min_ms": 300,
            "supported_max_ms": 5000,
            "last_adjusted_by": raw.get("last_adjusted_by") or "default",
            "last_adjusted_at": raw.get("last_adjusted_at"),
            "consumer_contract": {
                "mcp_should_tune": adaptive_enabled,
                "increase_when": [
                    "RESULT_INCOMPLETE",
                    "frame_age_p95 approaches/exceeds dwell",
                    "vision/decode service time approaches/exceeds dwell",
                    "frozen/dropped HDMI frames increase",
                ],
                "decrease_when": [
                    "recent optical deliveries are healthy",
                    "capture/decode latency is comfortably below dwell",
                ],
                "rule": "MCP consumers should call adaptive tuning before large optical reads and after transport degradation; decrease gradually after sustained healthy delivery.",
            },
        }

    def configure_optical_transport(
        self,
        *,
        adaptive_enabled: bool | None = None,
        min_ms: int | None = None,
        max_ms: int | None = None,
        current_ms: int | None = None,
        step_ms: int | None = None,
        adjusted_by: str = "easychange_ui",
    ) -> dict:
        """Persist bounded EC2 timing without YAML/database configuration."""
        current = self.optical_transport_config()

        def bounded_int(name: str, value, low: int, high: int) -> int:
            if isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be an integer") from exc
            if not low <= parsed <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
            return parsed

        next_min = current["min_ms"] if min_ms is None else bounded_int("min_ms", min_ms, 300, 5000)
        next_max = current["max_ms"] if max_ms is None else bounded_int("max_ms", max_ms, 300, 5000)
        if next_min > next_max:
            raise ValueError("min_ms must be <= max_ms")
        next_current = (
            current["current_ms"]
            if current_ms is None
            else bounded_int("current_ms", current_ms, 300, 5000)
        )
        if not next_min <= next_current <= next_max:
            raise ValueError("current_ms must be within min_ms/max_ms")
        next_step = (
            current["step_ms"]
            if step_ms is None
            else bounded_int("step_ms", step_ms, 25, 500)
        )
        if adaptive_enabled is None:
            next_adaptive = bool(current["adaptive_enabled"])
        elif isinstance(adaptive_enabled, bool):
            next_adaptive = adaptive_enabled
        else:
            raise ValueError("adaptive_enabled must be boolean")

        self.state_store.data["optical_transport"] = {
            "adaptive_enabled": next_adaptive,
            "min_ms": next_min,
            "max_ms": next_max,
            "current_ms": next_current,
            "step_ms": next_step,
            "last_adjusted_by": str(adjusted_by or "unknown")[:64],
            "last_adjusted_at": time.time(),
        }
        self._persist_state()
        return self.optical_transport_config()
    def _capability_manifest(self) -> dict:
        structured = structured_capabilities()
        supported_operations = sorted(structured.get("operations") or {})
        feature_flags = {
            "durable_results": True,
            "durable_result_chunks": True,
            "adaptive_result_rechunking": True,
            "durable_jobs": True,
            "selective_optical_chunks": True,
            "compressed_long_commands": True,
            "structured_payloads": True,
            "read_many_cursor": True,
            "git_diff_cursor": True,
            "runtime_env_refs": True,
            "secret_broker": True,
            "secret_source_import": True,
            "human_text_buffer": True,
            "ef_migrations": True,
            "database_aliases": True,
            "maintenance_cleanup": True,
            "browser_attach_existing": True,
            "browser_dom_paging": True,
            "browser_visual_qr": True,
            "swagger_browser_control": True,
            "browser_launch_allowed": False,
            "browser_profile_creation_allowed": False,
            "operation_profiles": True,
            "adaptive_optical_dwell": True,
            "mcp_optical_tuning": True,
            "compact_runtime_environment_status": True,
        }
        fingerprint_source = {
            "core_version": __version__,
            "protocol_version": "EC2",
            "structured_protocol_version": "easychange.structured/2",
            "job_protocol_version": "easychange.jobs/2",
            "secret_protocol_version": "easychange.secret/1",
            "db_protocol_version": "easychange.db/1",
            "supported_operations": supported_operations,
            "operation_profiles": structured.get("operations") or {},
            "feature_flags": feature_flags,
            "optical_transport_contract": {
                "adaptive": True,
                "configurable_in_easychange_ui": True,
                "mcp_tuning_supported": True,
                "initial_broadcast_max_chunks": 15,
                "overflow_broadcast_chunks": 3,
                "initial_broadcast_pages": 1,
                "delivery_strategy": "direct_up_to_15_chunks_else_header_plus_3_then_durable_result_store",
                "supported_min_ms": 300,
                "supported_max_ms": 5000,
                "default_min_ms": 600,
                "default_max_ms": 1800,
                "default_current_ms": 900,
                "guidance": "MCP should tune dwell to measured HDMI/decode latency: increase after incomplete delivery and decrease gradually after sustained healthy delivery.",
            },
            "runtime_environment_contract": {
                "compact_status_operation": "runtime_environment_status",
                "secret_values_returned": False,
                "guidance": "Use runtime_environment_status for confirmation of env-ref resolution. Use runtime_environment only when the complete ref/key metadata is actually required.",
            },
            "result_chunk_recovery_contract": {
                "adaptive_rechunking": True,
                "compressed_chunks_supported": True,
                "compressed_request_field": "compressed",
                "compressed_encoding": "zlib+base64",
                "compressed_stream_supported": True,
                "compressed_stream_operations": [
                    "result_stream_meta", "result_stream_chunk", "result_stream_chunks"
                ],
                "compressed_stream_encoding": "zlib-json-v1",
                "chunk_bytes_min": 256,
                "chunk_bytes_max": 4096,
                "index_geometry_depends_on_chunk_bytes": True,
                "guidance": "When one logical chunk fails optically, preserve its original byte interval and translate that interval to smaller chunk indexes. Do not reuse the same chunk index blindly after changing chunk_bytes.",
            },
        }
        digest = hashlib.sha256(
            json.dumps(
                fingerprint_source,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "capability_version": 2,
            "core_version": __version__,
            "protocol_version": "EC2",
            "structured_protocol_version": "easychange.structured/2",
            "job_protocol_version": "easychange.jobs/2",
            "secret_protocol_version": "easychange.secret/1",
            "db_protocol_version": "easychange.db/1",
            "supported_operations": supported_operations,
            "unsupported_operations": [],
            "operation_profiles": structured.get("operations") or {},
            "feature_flags": feature_flags,
            "optical_transport": self.optical_transport_config(),
            "optical_transport_contract": fingerprint_source["optical_transport_contract"],
            "runtime_environment_contract": fingerprint_source["runtime_environment_contract"],
            "result_chunk_recovery_contract": fingerprint_source["result_chunk_recovery_contract"],
            "max_payload": {
                "decoded_command_bytes": 262144,
                "structured_operations": int(structured.get("max_operations") or 64),
                "read_many_files": 64,
                "read_regions": 64,
                "discover_terms": 32,
            },
            "max_result": {
                "durable_results": 128,
                "durable_ttl_seconds": 1800,
                "logical_chunk_bytes_min": 256,
                "logical_chunk_bytes_max": 4096,
                "logical_chunk_rechunkable": True,
                "logical_chunk_compression": "optional-zlib+base64",
                "compressed_stream_recovery": "zlib-json-v1",
                "compressed_stream_default_chunk_bytes": 2048,
                "compressed_stream_default_max_ratio": 0.85,
            },
            "long_text_support": True,
            "structured_payload_support": True,
            "capabilities_sha256": digest,
        }

    def _ensure_index_freshness(self) -> dict:
        """Demand-refresh Git deltas before index-backed discovery.

        A warm SQLite index previously stayed stale until callers explicitly
        enabled :watch. Use the incremental Git watcher synchronously and at a
        bounded cadence; non-Git workspaces keep their existing explicit-watch
        behavior because a full filesystem refresh is materially more expensive.
        """
        if not (self.workspace.root_path / ".git").exists():
            return {
                "freshness_poll": False,
                "backend": "non-git-explicit-watch",
            }
        if self.watcher is None:
            self.watcher = WorkspaceWatcher(self.indexer)
        try:
            result = self.watcher.poll_if_due(0.75)
            return dict(result)
        except (OSError, RuntimeError) as exc:
            # Search still has cold git-grep / existing index fallbacks. A
            # transient watcher failure must be visible but not make discovery
            # unusable.
            return {
                "freshness_poll": False,
                "backend": "git-incremental",
                "error": f"{type(exc).__name__}:{str(exc)[:300]}",
            }

    def _dispatch(self, name: str, args: list[str]) -> dict:
        index_sensitive = {
            "search", "locate", "study", "discover-many",
            "symbols", "symbol", "definition", "references",
            "implementations", "outline",
        }
        if name in {
            "files", "tree", "projects", "next", "prev",
            *index_sensitive,
        } and self._transaction_dirty_paths:
            # Preserve read-after-write semantics while allowing mutation-only
            # batches to index each changed file once at commit.
            self._flush_transaction_index()
        if name in index_sensitive:
            self._ensure_index_freshness()
        if name == "command-schema":
            _require(args, 1, "command")
            if len(args) != 1:
                raise ValueError("command-schema requires exactly one command")
            return {"schema": command_schema(args[0])}
        if name == "help" and args:
            if len(args) != 1:
                raise ValueError("help accepts at most one command")
            return {"schema": command_schema(args[0])}
        if name in {"help", "capabilities"}:
            manifest = self._capability_manifest()
            if name == "capabilities" and "--compact" in args:
                return {"capability_manifest": manifest}
            return {"commands": list(COMMAND_NAMES),
                    "capability_manifest": manifest,
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
            compact = "--compact" in args or "compact" in args
            index = self.indexer.status()
            processes = self.processes.list()
            active_processes = sum(
                1 for item in processes
                if str(item.get("state") or "").upper() == "RUNNING"
            )
            transaction_state = "ACTIVE" if self.transaction is not None else "NONE"
            result_root_ready = bool(
                self.result_store.root.exists()
                and self.result_store.root.is_dir()
            )
            try:
                result_temp_files = sum(
                    1 for _ in self.result_store.root.glob("*.tmp")
                )
            except OSError:
                result_temp_files = -1

            degraded_reasons = []
            if transaction_state == "ACTIVE":
                degraded_reasons.append("TRANSACTION_ACTIVE")
            index_state = str(
                index.get("state") or index.get("status") or ""
            ).upper()
            if index_state in {"FAILED", "ERROR"}:
                degraded_reasons.append("INDEX_ERROR")
            if not result_root_ready:
                degraded_reasons.append("DURABLE_RESULT_STORE_UNAVAILABLE")

            if compact:
                # The MCP readiness probe must stay tiny both on the wire and in
                # execution cost. Do not compute browser diagnostics, SecretBroker
                # inventory, maintenance scans or capability manifests here.
                return {
                    "healthy": not degraded_reasons,
                    "state": "READY" if not degraded_reasons else "DEGRADED",
                    "workspace": str(self.workspace.root_path),
                    "workspace_type": self.workspace.kind,
                    "transaction": transaction_state,
                    "machine_mode": self.machine,
                    "transport": self.state_store.data.get("transport", "local"),
                    "process_count": len(processes),
                    "active_processes": active_processes,
                    "durable_results": result_root_ready,
                    "subsystems": {
                        "index": {
                            "healthy": index_state not in {"FAILED", "ERROR"},
                            "state": index_state or "UNKNOWN",
                        },
                        "durable_results": {
                            "healthy": result_root_ready,
                            "state": "READY" if result_root_ready else "UNAVAILABLE",
                        },
                        "processes": {
                            "healthy": True,
                            "state": "READY",
                        },
                    },
                    "degraded_reasons": degraded_reasons,
                    "compact": True,
                }

            maintenance = self.maintenance_report()
            browser_health = self.browser.health_summary()
            broker_status = self.secret_broker.status()
            if not bool(browser_health.get("healthy", True)):
                degraded_reasons.append("BROWSER_TRANSPORT_DEGRADED")
            disk_budget = (
                maintenance.get("disk_budget")
                if isinstance(maintenance, dict)
                and isinstance(maintenance.get("disk_budget"), dict)
                else {}
            )
            if bool(disk_budget.get("over_budget")):
                degraded_reasons.append("EASYCHANGE_DISK_BUDGET_EXCEEDED")

            subsystems = {
                "index": {
                    "healthy": index_state not in {"FAILED", "ERROR"},
                    "state": index_state or "UNKNOWN",
                },
                "durable_results": {
                    "healthy": result_root_ready,
                    "state": "READY" if result_root_ready else "UNAVAILABLE",
                    "temporary_files": result_temp_files,
                    "max_results": self.result_store.max_results,
                    "ttl_seconds": self.result_store.ttl_seconds,
                    "active_ttl_seconds": self.result_store.active_ttl_seconds,
                },
                "processes": {
                    "healthy": True,
                    "state": "READY",
                    "count": len(processes),
                    "active": active_processes,
                },
                "secret_broker": {
                    "healthy": True,
                    "state": "READY",
                    "count": int(broker_status.get("count") or 0),
                    "expires_at": broker_status.get("expires_at"),
                    "secret_values_returned": False,
                },
                "browser_transport": browser_health,
                "maintenance": {
                    "healthy": not bool(disk_budget.get("over_budget")),
                    "state": (
                        "DEGRADED"
                        if bool(disk_budget.get("over_budget"))
                        else "READY"
                    ),
                    "disk_budget": disk_budget,
                },
            }
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
                "durable_results": result_root_ready,
                "subsystems": subsystems,
                "capability_manifest": self._capability_manifest(),
                "structured_operations": structured_capabilities(),
                "maintenance": maintenance,
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
            cursor_value = _option_value(args, "--cursor")
            option_names = {"--count", "--max-bytes", "--cursor"}
            paths = []
            skip = False
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

            cursor_path_index = 0
            cursor_start = 1
            cursor_remaining = count
            if cursor_value:
                match = re.fullmatch(r"v1:(\d+):(\d+):(\d+)", str(cursor_value))
                if not match:
                    raise ValueError("INVALID_READ_MANY_CURSOR")
                cursor_path_index = int(match.group(1))
                cursor_start = max(1, int(match.group(2)))
                cursor_remaining = max(1, min(count, int(match.group(3))))
                if cursor_path_index >= len(paths):
                    raise ValueError("READ_MANY_CURSOR_OUT_OF_RANGE")

            active_specs = []
            for absolute_index in range(cursor_path_index, len(paths)):
                start_line = cursor_start if absolute_index == cursor_path_index else 1
                requested_lines = cursor_remaining if absolute_index == cursor_path_index else count
                active_specs.append((absolute_index, paths[absolute_index], start_line, requested_lines))

            files = []
            skipped = []
            errors = []
            total_bytes = 0
            workers = min(12, len(active_specs))

            def safe_read(spec):
                absolute_index, path, start_line, requested_lines = spec
                try:
                    return spec, self.files.read(path, start_line, requested_lines), None
                except Exception as exc:
                    return spec, None, {
                        "path": path,
                        "error": str(exc)[:512],
                        "code": str(getattr(exc, "code", "") or type(exc).__name__),
                    }

            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="easychange-read") as pool:
                loaded = list(pool.map(safe_read, active_specs))

            next_cursor = None
            for position, (spec, info, error) in enumerate(loaded):
                absolute_index, path, start_line, requested_lines = spec
                if error is not None:
                    errors.append(error)
                    continue
                assert info is not None
                encoded = json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if total_bytes + len(encoded) > max_bytes:
                    if not files:
                        # Trim the first page deterministically. The cursor
                        # remembers exactly how many requested lines remain.
                        lines = list(info.get("lines") or [])
                        while len(encoded) > max_bytes and len(lines) > 1:
                            lines = lines[:max(1, len(lines) // 2)]
                            info = {
                                **info,
                                "lines": lines,
                                "returned_lines": len(lines),
                                "budget_truncated": True,
                                "has_more": True,
                                "next_start": start_line + len(lines),
                            }
                            encoded = json.dumps(
                                info, ensure_ascii=False, separators=(",", ":")
                            ).encode("utf-8")
                        if len(encoded) > max_bytes:
                            errors.append({
                                "path": path,
                                "error": "Single source line exceeds read-many byte budget",
                                "code": "LINE_EXCEEDS_BYTE_BUDGET",
                                "start": start_line,
                                "byte_budget": max_bytes,
                            })
                            next_index = absolute_index + 1
                            if next_index < len(paths):
                                next_cursor = f"v1:{next_index}:1:{count}"
                        else:
                            files.append(info)
                            total_bytes += len(encoded)
                            consumed = len(lines)
                            remaining_lines = max(0, requested_lines - consumed)
                            if remaining_lines > 0:
                                next_cursor = (
                                    f"v1:{absolute_index}:{start_line + consumed}:{remaining_lines}"
                                )
                            else:
                                next_index = absolute_index + 1
                                if next_index < len(paths):
                                    next_cursor = f"v1:{next_index}:1:{count}"
                        skipped.extend(
                            item[0][1]
                            for item in loaded[position + 1:]
                            if item[2] is None
                        )
                    else:
                        skipped.extend(
                            item[0][1]
                            for item in loaded[position:]
                            if item[2] is None
                        )
                        next_cursor = f"v1:{absolute_index}:{start_line}:{requested_lines}"
                    break

                files.append(info)
                total_bytes += len(encoded)

                # If FileService could not return the requested slice in one
                # page, preserve the exact continuation before advancing paths.
                returned_lines = int(info.get("returned_lines") or len(info.get("lines") or []))
                if bool(info.get("has_more")) and returned_lines < requested_lines:
                    remaining_lines = max(1, requested_lines - returned_lines)
                    next_cursor = (
                        f"v1:{absolute_index}:{start_line + returned_lines}:{remaining_lines}"
                    )
                    skipped.extend(
                        item[0][1]
                        for item in loaded[position + 1:]
                        if item[2] is None
                    )
                    break

            budget_truncated = (
                bool(next_cursor)
                or bool(skipped)
                or any(bool(item.get("budget_truncated")) for item in files)
            )
            complete = next_cursor is None
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
                "has_more": not complete,
                "remaining": len(skipped),
                "cursor": cursor_value,
                "next_cursor": next_cursor,
                "complete": complete,
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
            exact = str(prefix or "").lstrip(":").strip().casefold()
            if exact in self._commands():
                return {
                    "candidates": [":" + exact],
                    "schema": command_schema(exact),
                }
            if prefix.startswith(":open ") or prefix.startswith(":read "):
                part = prefix.split(maxsplit=1)[1]
                candidates = [item["path"] for item in self.indexer.files(limit=1000) if item["path"].startswith(part)]
            else:
                normalized_prefix = prefix if prefix.startswith(":") else ":" + prefix
                candidates = [
                    ":" + command
                    for command in self._commands()
                    if (":" + command).startswith(normalized_prefix)
                ]
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
                test_args = (
                    list(_profile_command(self.workspace, "test") or [])
                    if "DOTNET" in set(self.workspace.adapters or [])
                    else []
                )
                if test_args and "--no-build" not in test_args:
                    test_args.append("--no-build")
                if test_args and "--no-restore" not in test_args:
                    test_args.append("--no-restore")
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
            with self.transaction_path.open("rb") as transaction_header:
                raw = transaction_header.read(MAX_ACTIVE_TRANSACTION_HEADER_BYTES + 1)
            if len(raw) > MAX_ACTIVE_TRANSACTION_HEADER_BYTES:
                raise ValueError("ACTIVE_TRANSACTION_HEADER_TOO_LARGE")
            value = json.loads(raw.decode("utf-8"))
            if (not isinstance(value, dict) or
                    not isinstance(value.get("transaction_id"), str) or
                    not value["transaction_id"]):
                raise ValueError("INVALID_ACTIVE_TRANSACTION_HEADER")
            self.transaction_id = value["transaction_id"]
            if value.get("format") == "append-v2":
                changes: list[Change] = []
                if self.transaction_log_path.exists():
                    # A large active transaction can contain thousands of
                    # before/after snapshots. read_text().splitlines()
                    # materialized two extra copies of the entire journal.
                    # Stream records in order, retaining only real Changes.
                    with self.transaction_log_path.open("r", encoding="utf-8") as stream:
                        while True:
                            line = stream.readline(MAX_ACTIVE_TRANSACTION_RECORD_CHARS + 1)
                            if not line:
                                break
                            if len(line) > MAX_ACTIVE_TRANSACTION_RECORD_CHARS:
                                raise ValueError("ACTIVE_TRANSACTION_RECORD_TOO_LARGE")
                            if not line.strip():
                                continue
                            try:
                                item = json.loads(line)
                                changes.append(Change(**item))
                            except (json.JSONDecodeError, TypeError) as exc:
                                # An incomplete final append may represent an
                                # already-applied edit whose rollback evidence
                                # was only partly written. Skipping it permits
                                # unsafe commit/replay and appending onto the
                                # corrupt suffix. Preserve bytes and fail closed.
                                raise ValueError("ACTIVE_TRANSACTION_LOG_CORRUPT") from exc
                self.transaction = changes
                self._transaction_persisted_count = len(changes)
                self._transaction_storage_v2 = True
            else:
                # Backward compatibility with the original monolithic JSON
                # transaction file. The next save migrates it to append-v2.
                self.transaction = [Change(**item) for item in value.get("changes", [])]
                self._transaction_persisted_count = 0
                self._transaction_storage_v2 = False
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.transaction_id = None
            self.transaction = None
            self._transaction_persisted_count = 0
            self._transaction_storage_v2 = False
            # A present but unreadable transaction is NOT an empty transaction.
            # Preserve the journal and refuse new edits until it is recovered.
            raise RuntimeError("ACTIVE_TRANSACTION_RECOVERY_FAILED") from exc

    def _save_transaction(self) -> None:
        """Persist active transaction in O(new changes), not O(total changes)."""
        if self.transaction is None:
            return
        self.transaction_path.parent.mkdir(parents=True, exist_ok=True)

        if not self._transaction_storage_v2:
            # Migration from monolithic JSON must publish the COMPLETE v2 log
            # before replacing the legacy header. Otherwise a crash after
            # header replacement but before append erases rollback evidence.
            # Each staging file is unique and fsynced; no full-log JSON copy.
            journal_fd, journal_name = tempfile.mkstemp(
                prefix=self.transaction_log_path.name + ".", suffix=".tmp",
                dir=self.transaction_path.parent,
            )
            try:
                try:
                    journal_stream = os.fdopen(journal_fd, "w", encoding="utf-8", newline="\n")
                except BaseException:
                    os.close(journal_fd)
                    raise
                with journal_stream as stream:
                    for change in self.transaction:
                        stream.write(json.dumps({
                            "path": change.path, "before": change.before,
                            "after": change.after, "change_id": change.change_id,
                            "command": change.command,
                        }, ensure_ascii=False, separators=(",", ":")) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(journal_name, self.transaction_log_path)
                if os.name == "posix":
                    # Sync directory metadata before publishing a header that
                    # depends on this newly renamed log. File fsync alone does
                    # not guarantee the rename survives a power loss.
                    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    directory_fd = os.open(self.transaction_path.parent, flags)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                Path(journal_name).unlink(missing_ok=True)

            header_fd, header_name = tempfile.mkstemp(
                prefix=self.transaction_path.name + ".", suffix=".tmp",
                dir=self.transaction_path.parent,
            )
            try:
                try:
                    header_stream = os.fdopen(header_fd, "w", encoding="utf-8")
                except BaseException:
                    os.close(header_fd)
                    raise
                with header_stream as stream:
                    stream.write(json.dumps(
                        {"format": "append-v2", "transaction_id": self.transaction_id},
                        ensure_ascii=False, separators=(",", ":"),
                    ))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(header_name, self.transaction_path)
            finally:
                Path(header_name).unlink(missing_ok=True)
            self._transaction_persisted_count = len(self.transaction)
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
            # Only one flush/fsync per save batch, never per line. A successful
            # mutation must not return while its rollback journal is merely
            # buffered in user space or the OS page cache.
            stream.flush()
            os.fsync(stream.fileno())
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
