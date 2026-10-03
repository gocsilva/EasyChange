from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .database_service import DatabaseService
from .process_service import ProcessService
from .runtime_service import ProjectRuntimeService
from .workspace import Workspace


_NOISE_RE = re.compile(
    r"^(?:Build started|Build succeeded|Done\.|info:|warn:|warning:|error:)",
    re.IGNORECASE,
)


class EfMigrationService:
    """Deterministic EF Core migration workflow.

    Secrets are resolved only into child-process/database environments by the
    existing runtime/database services. They are never added to argv or returned.
    """

    def __init__(
        self,
        workspace: Workspace,
        processes: ProcessService,
        runtime: ProjectRuntimeService,
        database: DatabaseService,
    ) -> None:
        self.workspace = workspace
        self.processes = processes
        self.runtime = runtime
        self.database = database
        self.script_root = processes.root.parent / "ef-scripts"
        self.script_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _dotnet() -> str:
        return shutil.which("dotnet") or "dotnet"

    def _project(self, value: str | None, label: str) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        path = self.workspace.resolve(text, must_exist=True)
        if not path.is_file() or path.suffix.casefold() != ".csproj":
            raise ValueError(f"{label}_MUST_BE_CSPROJ")
        return path.relative_to(self.workspace.root_path).as_posix()

    def _execution_environment(
        self,
        profile_id: str = "",
        environment: str = "",
        *,
        external_env: dict[str, str] | None = None,
    ) -> tuple[dict[str, str] | None, list[str], dict[str, Any]]:
        profile = None
        if profile_id:
            profile = self.runtime.profile(profile_id)
            env, metadata = self.runtime.resolve_environment(
                profile,
                external_env=external_env,
                require_all=True,
            )
            redact = self.runtime._redaction_values(profile, env)
        else:
            env = dict(os.environ)
            metadata = {
                "inherit_env": True,
                "env_allowlist": [],
                "resolved_env_keys": [],
                "unresolved_env_refs": [],
                "secret_values_returned": False,
            }
            redact = []
        environment = str(environment or "").strip()
        if environment:
            env["ASPNETCORE_ENVIRONMENT"] = environment
            keys = set(metadata.get("resolved_env_keys") or [])
            keys.add("ASPNETCORE_ENVIRONMENT")
            metadata = {**metadata, "resolved_env_keys": sorted(keys)}
        return env, redact, {
            **metadata,
            "profile_id": profile.get("id") if isinstance(profile, dict) else None,
            "environment": environment or None,
        }

    @staticmethod
    def _ensure_ok(result: dict, code: str) -> dict:
        if int(result.get("returncode") or 0) != 0:
            raise RuntimeError(code + ":" + str(result.get("diagnostics") or result.get("stderr") or "")[-1600:])
        return result

    def _ef_args(
        self,
        *parts: str,
        migration_project: str | None,
        startup_project: str | None,
        context: str | None,
        no_build: bool = True,
    ) -> list[str]:
        argv = [self._dotnet(), "ef", *parts]
        if migration_project:
            argv += ["--project", migration_project]
        if startup_project:
            argv += ["--startup-project", startup_project]
        if context:
            argv += ["--context", str(context)]
        if no_build:
            argv.append("--no-build")
        return argv

    @staticmethod
    def _lines(result: dict) -> list[str]:
        output = "\n".join([
            str(result.get("stdout") or ""),
            str(result.get("stderr") or ""),
        ])
        rows: list[str] = []
        for raw in output.splitlines():
            line = raw.strip()
            if not line or _NOISE_RE.match(line):
                continue
            if line.casefold().startswith("no migrations"):
                continue
            rows.append(line)
        return rows

    def _build(
        self,
        migration_project: str | None,
        startup_project: str | None,
        *,
        env: dict[str, str] | None,
        redact: list[str],
        profile_id: str = "",
        timeout: int = 300,
    ) -> dict:
        if profile_id:
            profile = self.runtime.profile(profile_id)
            configured = profile.get("build_argv")
            if configured:
                argv = list(configured)
            else:
                argv = [self._dotnet(), "build", startup_project or migration_project or "."]
        else:
            argv = [self._dotnet(), "build", startup_project or migration_project or "."]
        result = self.processes.run(
            argv,
            timeout=max(30, min(1800, int(timeout))),
            env=env,
            redact_values=redact,
        )
        self._ensure_ok(result, "EF_BUILD_FAILED")
        return {
            "return_code": result.get("returncode"),
            "duration_ms": result.get("duration_ms"),
            "diagnostics": result.get("diagnostics") or "",
            "output_truncated": bool(result.get("output_truncated")),
        }

    def _contexts(
        self,
        migration_project: str | None,
        startup_project: str | None,
        *,
        env: dict[str, str] | None,
        redact: list[str],
        timeout: int,
    ) -> list[str]:
        result = self.processes.run(
            self._ef_args(
                "dbcontext", "list",
                migration_project=migration_project,
                startup_project=startup_project,
                context=None,
            ),
            timeout=timeout,
            env=env,
            redact_values=redact,
        )
        self._ensure_ok(result, "EF_DBCONTEXT_LIST_FAILED")
        return self._lines(result)

    def _migrations(
        self,
        migration_project: str | None,
        startup_project: str | None,
        context: str | None,
        *,
        env: dict[str, str] | None,
        redact: list[str],
        timeout: int,
    ) -> list[str]:
        result = self.processes.run(
            self._ef_args(
                "migrations", "list",
                migration_project=migration_project,
                startup_project=startup_project,
                context=context,
            ),
            timeout=timeout,
            env=env,
            redact_values=redact,
        )
        self._ensure_ok(result, "EF_MIGRATIONS_LIST_FAILED")
        return self._lines(result)

    @staticmethod
    def _history_ids(result: dict) -> list[str]:
        ids: list[str] = []
        for row in result.get("rows") or []:
            if isinstance(row, (list, tuple)) and row:
                value = str(row[0]).strip()
            else:
                value = str(row).split("|", 1)[0].strip()
            if value and value not in ids:
                ids.append(value)
        return ids

    def _history(self, connection_alias: str) -> dict:
        result = self.database.query(
            connection_alias,
            "SELECT MigrationId, ProductVersion FROM __EFMigrationsHistory ORDER BY MigrationId",
            allow_write=False,
            max_rows=5000,
            timeout=30,
        )
        return {
            "connection_alias": connection_alias,
            "applied_migrations": self._history_ids(result),
            "row_count": int(result.get("row_count") or 0),
            "truncated": bool(result.get("truncated")),
        }

    @staticmethod
    def _normalize_migration_name(value: str) -> str:
        # Strip EF's optional '(Pending)' marker but preserve the migration id.
        return re.sub(r"\s+\(Pending\)\s*$", "", str(value or "").strip(), flags=re.I)

    def migrations_list(
        self,
        *,
        migration_project: str,
        startup_project: str | None = None,
        context: str | None = None,
        environment: str = "",
        connection_alias: str = "",
        profile_id: str = "",
        timeout: int = 300,
        external_env: dict[str, str] | None = None,
    ) -> dict:
        migration = self._project(migration_project, "MIGRATION_PROJECT")
        startup = self._project(startup_project, "STARTUP_PROJECT") if startup_project else None
        env, redact, env_meta = self._execution_environment(
            profile_id, environment, external_env=external_env
        )
        build = self._build(
            migration, startup,
            env=env, redact=redact, profile_id=profile_id, timeout=timeout,
        )
        contexts = self._contexts(
            migration, startup, env=env, redact=redact, timeout=timeout,
        )
        chosen_context = str(context or "").strip() or None
        if chosen_context:
            normalized = {item.rsplit(".", 1)[-1].casefold() for item in contexts}
            normalized.update(item.casefold() for item in contexts)
            if chosen_context.casefold() not in normalized:
                raise LookupError(f"EF_DBCONTEXT_NOT_FOUND:{chosen_context}")
        elif len(contexts) == 1:
            chosen_context = contexts[0]
        elif len(contexts) > 1:
            raise RuntimeError("EF_CONTEXT_REQUIRED_MULTIPLE_FOUND")

        migrations = [
            self._normalize_migration_name(item)
            for item in self._migrations(
                migration, startup, chosen_context,
                env=env, redact=redact, timeout=timeout,
            )
        ]
        history = None
        applied: list[str] = []
        if connection_alias:
            history = self._history(connection_alias)
            if history["truncated"]:
                raise RuntimeError("EF_HISTORY_TRUNCATED")
            applied = list(history["applied_migrations"])
        pending = [item for item in migrations if item not in set(applied)] if history else []
        return {
            "migration_project": migration,
            "startup_project": startup,
            "context": chosen_context,
            "contexts": contexts,
            "migrations": migrations,
            "migration_count": len(migrations),
            "history_checked": bool(history),
            "history": history,
            "pending_migrations": pending,
            "pending_count": len(pending) if history else None,
            "build": build,
            "environment": env_meta,
            "secret_values_returned": False,
        }

    def database_update(
        self,
        *,
        migration_project: str,
        startup_project: str | None = None,
        context: str | None = None,
        environment: str = "",
        connection_alias: str,
        target_migration: str = "",
        profile_id: str = "",
        apply_authorized: bool = False,
        timeout: int = 600,
        external_env: dict[str, str] | None = None,
    ) -> dict:
        if not apply_authorized:
            raise PermissionError("EF_DATABASE_UPDATE_REQUIRES_EXPLICIT_AUTHORIZATION")
        if not str(connection_alias or "").strip():
            raise ValueError("EF_DATABASE_UPDATE_REQUIRES_CONNECTION_ALIAS")

        before = self.migrations_list(
            migration_project=migration_project,
            startup_project=startup_project,
            context=context,
            environment=environment,
            connection_alias=connection_alias,
            profile_id=profile_id,
            timeout=timeout,
            external_env=external_env,
        )
        target = str(target_migration or "").strip()
        if not target and before.get("pending_count") == 0:
            return {
                **before,
                "applied": False,
                "reason": "NO_PENDING_MIGRATIONS",
                "history_before": before.get("history"),
                "history_after": before.get("history"),
            }

        env, redact, env_meta = self._execution_environment(
            profile_id, environment, external_env=external_env
        )
        argv = self._ef_args(
            "database", "update",
            migration_project=before["migration_project"],
            startup_project=before["startup_project"],
            context=before["context"],
        )
        if target:
            argv.insert(4, target)
        execution = self.processes.run(
            argv,
            timeout=max(30, min(3600, int(timeout))),
            env=env,
            redact_values=redact,
        )
        self._ensure_ok(execution, "EF_DATABASE_UPDATE_FAILED")
        history_after = self._history(connection_alias)
        if history_after["truncated"]:
            raise RuntimeError("EF_HISTORY_TRUNCATED_AFTER_UPDATE")

        expected = target or (before["migrations"][-1] if before["migrations"] else None)
        verified = bool(
            expected is None
            or expected in set(history_after["applied_migrations"])
        )
        if not verified:
            raise RuntimeError("EF_DATABASE_UPDATE_NOT_VERIFIED")
        return {
            "migration_project": before["migration_project"],
            "startup_project": before["startup_project"],
            "context": before["context"],
            "target_migration": target or None,
            "applied": True,
            "verified": True,
            "return_code": execution.get("returncode"),
            "duration_ms": execution.get("duration_ms"),
            "history_before": before.get("history"),
            "history_after": history_after,
            "environment": env_meta,
            "secret_values_returned": False,
        }

    def script(
        self,
        *,
        migration_project: str,
        startup_project: str | None = None,
        context: str | None = None,
        environment: str = "",
        target_migration: str = "",
        profile_id: str = "",
        timeout: int = 300,
        cursor: int = 0,
        max_bytes: int = 16384,
        external_env: dict[str, str] | None = None,
    ) -> dict:
        preflight = self.migrations_list(
            migration_project=migration_project,
            startup_project=startup_project,
            context=context,
            environment=environment,
            connection_alias="",
            profile_id=profile_id,
            timeout=timeout,
            external_env=external_env,
        )
        env, redact, env_meta = self._execution_environment(
            profile_id, environment, external_env=external_env
        )
        seed = "|".join([
            preflight["migration_project"] or "",
            preflight["startup_project"] or "",
            preflight["context"] or "",
            str(target_migration or ""),
        ])
        script_id = "EFS" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16].upper()
        path = self.script_root / f"{script_id}.sql"
        argv = self._ef_args(
            "migrations", "script",
            migration_project=preflight["migration_project"],
            startup_project=preflight["startup_project"],
            context=preflight["context"],
        )
        if target_migration:
            argv.insert(4, str(target_migration))
        argv += ["--output", str(path)]
        execution = self.processes.run(
            argv,
            timeout=max(30, min(1800, int(timeout))),
            env=env,
            redact_values=redact,
        )
        self._ensure_ok(execution, "EF_MIGRATIONS_SCRIPT_FAILED")
        raw = path.read_bytes()
        start = max(0, min(len(raw), int(cursor or 0)))
        limit = max(512, min(1024 * 1024, int(max_bytes or 16384)))
        chunk = raw[start:start + limit]
        next_cursor = start + len(chunk)
        complete = next_cursor >= len(raw)
        return {
            "script_id": script_id,
            "migration_project": preflight["migration_project"],
            "startup_project": preflight["startup_project"],
            "context": preflight["context"],
            "target_migration": str(target_migration or "") or None,
            "text": chunk.decode("utf-8", errors="replace"),
            "cursor": start,
            "next_cursor": None if complete else next_cursor,
            "returned_bytes": len(chunk),
            "total_bytes": len(raw),
            "remaining_bytes": max(0, len(raw) - next_cursor),
            "complete": complete,
            "environment": env_meta,
            "secret_values_returned": False,
        }
