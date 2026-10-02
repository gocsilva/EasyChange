from __future__ import annotations
import tempfile

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from .workspace import Workspace

_READ_ONLY = re.compile(r"^\s*(?:--[^\n]*\n\s*)*(select|with|pragma|explain|show|describe)\b", re.I)


class DatabaseService:
    """Workspace-local SQL helper. Read-only by default; secrets remain in environment variables."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.config_path = workspace.root_path / ".easychange" / "db_connections.json"

    def connections(self) -> dict:
        value = self._config()
        sanitized = {}
        for name, cfg in value.items():
            sanitized[name] = {key: val for key, val in cfg.items()
                               if key not in {"password", "token", "secret"} and not key.endswith("_value")}
        return {"connections": sanitized, "count": len(sanitized), "config": ".easychange/db_connections.json"}

    def configure(self, name: str, provider: str, settings: dict | None = None) -> dict:
        """Persist a connection alias without accepting secret values."""
        name = str(name or "").strip()
        provider = str(provider or "").strip().casefold()
        if not name:
            raise ValueError("CONNECTION_NAME_REQUIRED")
        if provider not in {"sqlite", "sqlite3", "sqlserver", "mssql", "postgres", "postgresql", "mysql", "mariadb"}:
            raise ValueError("UNSUPPORTED_DATABASE_PROVIDER")
        settings = dict(settings or {})
        forbidden = {"password", "token", "secret", "connection_string", "connectionstring"}
        if any(str(key).casefold() in forbidden or str(key).casefold().endswith("_value") for key in settings):
            raise ValueError("SECRET_VALUES_NOT_ALLOWED_USE_ENV_NAME")
        allowed = {
            "path", "server", "database", "user", "host", "port", "trusted",
            "server_env", "database_env", "user_env", "host_env", "port_env", "password_env",
        }
        row = {"provider": provider}
        for key, value in settings.items():
            if key in allowed and value is not None and value != "":
                row[key] = value
        config = self._config()
        config[name] = row
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({"connections": config}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"name": name, "connection": {k: v for k, v in row.items() if k != "password"},
                "config": ".easychange/db_connections.json"}

    def _config(self) -> dict[str, dict]:
        if not self.config_path.exists():
            return {}
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        connections = value.get("connections") if isinstance(value, dict) else None
        return connections if isinstance(connections, dict) else {}

    def _connection(self, name: str) -> dict:
        config = self._config()
        if name in config and isinstance(config[name], dict):
            return dict(config[name])
        if name.casefold().endswith((".db", ".sqlite", ".sqlite3")):
            return {"provider": "sqlite", "path": name}
        raise LookupError(f"Database connection not found: {name}")

    @staticmethod
    def _is_read_only(sql: str) -> bool:
        """Conservative SQL classifier for the default read-only path.

        This is intentionally stricter than checking only the first token:
        mutable CTEs, SELECT INTO, multiple statements containing DML/DDL,
        and execution primitives are rejected unless allow_write=True.
        """
        text = str(sql or "").strip()
        if not text:
            return False
        sanitized = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        sanitized = re.sub(r"--[^\r\n]*", " ", sanitized)
        sanitized = re.sub(r"'(?:''|[^'])*'", "''", sanitized)
        sanitized = re.sub(r'"(?:""|[^"])*"', '""', sanitized)
        sanitized = re.sub(r"\[[^\]]*\]", "[]", sanitized)
        normalized = re.sub(r"\s+", " ", sanitized).strip().casefold()

        dangerous = (
            r"\b(?:insert|update|delete|merge|upsert|replace|drop|alter|create|truncate|"
            r"grant|revoke|execute|exec|call|copy|vacuum|attach|detach|reindex|"
            r"load\s+data|outfile|dumpfile)\b"
        )
        if re.search(dangerous, normalized):
            return False
        if re.search(r"\bselect\b[\s\S]*\binto\b", normalized):
            return False
        if re.search(r"\bfor\s+update\b", normalized):
            return False

        first = re.match(r"^([a-z]+)\b", normalized)
        keyword = first.group(1) if first else ""
        if keyword in {"select", "show", "describe", "desc"}:
            return True
        if keyword == "explain":
            return bool(re.search(r"\b(?:select|show|describe|desc)\b", normalized))
        if keyword == "with":
            return bool(re.search(r"\bselect\b", normalized))
        if keyword == "pragma":
            match = re.match(r"^pragma\s+([a-z0-9_]+)\b", normalized)
            safe = {
                "table_info", "table_xinfo", "index_info", "index_xinfo", "index_list",
                "foreign_key_list", "database_list", "compile_options", "function_list",
                "module_list", "pragma_list", "collation_list",
            }
            return bool(match and match.group(1) in safe and "=" not in normalized)
        return False

    def query(self, connection: str, sql: str, *, allow_write: bool = False,
              max_rows: int = 200, timeout: int = 30) -> dict[str, Any]:
        sql = str(sql or "").strip()
        if not sql:
            raise ValueError("SQL_REQUIRED")
        if not allow_write and not self._is_read_only(sql):
            raise PermissionError("WRITE_SQL_REQUIRES_ALLOW_WRITE")
        cfg = self._connection(connection)
        provider = str(cfg.get("provider") or "").casefold()
        if provider in {"sqlite", "sqlite3"}:
            result = self._sqlite_query(cfg, sql, allow_write=allow_write, max_rows=max_rows, timeout=timeout)
        elif provider in {"sqlserver", "mssql"}:
            result = self._sqlcmd_query(cfg, sql, max_rows=max_rows, timeout=timeout)
        elif provider in {"postgres", "postgresql"}:
            result = self._psql_query(cfg, sql, max_rows=max_rows, timeout=timeout)
        elif provider in {"mysql", "mariadb"}:
            result = self._mysql_query(cfg, sql, max_rows=max_rows, timeout=timeout)
        else:
            raise ValueError(f"Unsupported database provider: {provider}")
        result.setdefault("connection", connection)
        result.setdefault("read_only", not allow_write)
        result.setdefault("max_rows", max(1, min(5000, int(max_rows))))
        result.setdefault("has_more", bool(result.get("truncated")))
        return result

    def schema(self, connection: str, max_rows: int = 500) -> dict:
        cfg = self._connection(connection)
        provider = str(cfg.get("provider") or "").casefold()
        if provider in {"sqlite", "sqlite3"}:
            sql = "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table','view') ORDER BY type,name"
        elif provider in {"sqlserver", "mssql"}:
            sql = ("SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                   "FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_SCHEMA,TABLE_NAME,ORDINAL_POSITION")
        elif provider in {"postgres", "postgresql"}:
            sql = ("SELECT table_schema, table_name, column_name, data_type, is_nullable "
                   "FROM information_schema.columns WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                   "ORDER BY table_schema,table_name,ordinal_position")
        elif provider in {"mysql", "mariadb"}:
            sql = ("SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE,IS_NULLABLE "
                   "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                   "ORDER BY TABLE_NAME,ORDINAL_POSITION")
        else:
            raise ValueError(f"Unsupported database provider: {provider}")
        result = self.query(connection, sql, allow_write=False, max_rows=max_rows)
        result["schema"] = True
        return result

    def _sqlite_path(self, cfg: dict) -> Path:
        raw = str(cfg.get("path") or "")
        if not raw:
            raise ValueError("SQLITE_PATH_REQUIRED")
        return self.workspace.resolve(raw, must_exist=True)

    def _sqlite_query(self, cfg: dict, sql: str, *, allow_write: bool, max_rows: int, timeout: int) -> dict:
        path = self._sqlite_path(cfg)
        started = time.monotonic()
        connection = sqlite3.connect(path, timeout=max(1, timeout))
        try:
            if not allow_write:
                connection.execute("PRAGMA query_only=ON")
            cursor = connection.execute(sql)
            columns = [item[0] for item in (cursor.description or [])]
            rows = cursor.fetchmany(max(1, min(5000, int(max_rows))) + 1) if cursor.description else []
            truncated = len(rows) > max_rows
            rows = rows[:max_rows]
            if allow_write:
                connection.commit()
            return {"provider": "sqlite", "database": _rel(self.workspace.root_path, path),
                    "columns": columns, "rows": [list(row) for row in rows],
                    "row_count": len(rows), "truncated": truncated,
                    "changes": connection.total_changes,
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        finally:
            connection.close()

    @staticmethod
    def _secret_env(cfg: dict, provider_password_env: str) -> dict:
        env = os.environ.copy()
        name = str(cfg.get("password_env") or "")
        if name:
            value = os.environ.get(name)
            if value:
                env[provider_password_env] = value
        return env

    def _sqlcmd_query(self, cfg: dict, sql: str, *, max_rows: int, timeout: int) -> dict:
        exe = shutil.which("sqlcmd")
        if not exe:
            raise RuntimeError("sqlcmd not found")
        server_env = str(cfg.get("server_env") or "")
        database_env = str(cfg.get("database_env") or "")
        user_env = str(cfg.get("user_env") or "")
        server = str(cfg.get("server") or (os.environ.get(server_env) if server_env else "") or "")
        database = str(cfg.get("database") or (os.environ.get(database_env) if database_env else "") or "")
        user = str(cfg.get("user") or (os.environ.get(user_env) if user_env else "") or "")
        if not server:
            raise ValueError("SQLSERVER_SERVER_REQUIRED")
        argv = [exe, "-S", server, "-W", "-s", "|", "-h", "-1", "-Q", sql]
        if database:
            argv += ["-d", database]
        if bool(cfg.get("trusted", not user)):
            argv.append("-E")
        elif user:
            argv += ["-U", user]
        return self._cli_query("sqlserver", argv, self._secret_env(cfg, "SQLCMDPASSWORD"), max_rows, timeout)

    def _psql_query(self, cfg: dict, sql: str, *, max_rows: int, timeout: int) -> dict:
        exe = shutil.which("psql")
        if not exe:
            raise RuntimeError("psql not found")
        argv = [exe, "-At", "-F", "|", "-c", sql]
        for flag, key, env_key in (("-h", "host", "host_env"), ("-p", "port", "port_env"),
                                   ("-d", "database", "database_env"), ("-U", "user", "user_env")):
            env_name = str(cfg.get(env_key) or "")
            value = cfg.get(key) or (os.environ.get(env_name) if env_name else "")
            if value:
                argv += [flag, str(value)]
        return self._cli_query("postgresql", argv, self._secret_env(cfg, "PGPASSWORD"), max_rows, timeout)

    def _mysql_query(self, cfg: dict, sql: str, *, max_rows: int, timeout: int) -> dict:
        exe = shutil.which("mysql")
        if not exe:
            raise RuntimeError("mysql not found")
        argv = [exe, "--batch", "--raw", "--skip-column-names", "-e", sql]
        for flag, key, env_key in (("-h", "host", "host_env"), ("-P", "port", "port_env"),
                                   ("-D", "database", "database_env"), ("-u", "user", "user_env")):
            env_name = str(cfg.get(env_key) or "")
            value = cfg.get(key) or (os.environ.get(env_name) if env_name else "")
            if value:
                argv += [flag, str(value)]
        return self._cli_query("mysql", argv, self._secret_env(cfg, "MYSQL_PWD"), max_rows, timeout)

    @staticmethod
    def _cli_query(provider: str, argv: list[str], env: dict, max_rows: int, timeout: int) -> dict:
        """Run a DB CLI with disk-backed capture so large query output cannot exhaust RAM."""
        started = time.monotonic()
        max_rows = max(1, min(5000, int(max_rows)))

        with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
            completed = subprocess.run(
                argv,
                stdout=stdout_file,
                stderr=stderr_file,
                env=env,
                timeout=timeout,
                check=False,
                shell=False,
            )

            if completed.returncode:
                def tail_text(handle, limit: int = 2000) -> str:
                    handle.seek(0, 2)
                    size = handle.tell()
                    handle.seek(max(0, size - limit))
                    return handle.read().decode("utf-8", errors="replace")

                stderr = tail_text(stderr_file)
                stdout = tail_text(stdout_file)
                raise RuntimeError((stderr or stdout or f"{provider} query failed")[-2000:])

            stdout_file.seek(0)
            rows = []
            for _ in range(max_rows + 1):
                raw = stdout_file.readline()
                if not raw:
                    break
                rows.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))

            truncated = len(rows) > max_rows
            rows = rows[:max_rows]
            return {
                "provider": provider,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "capture_backend": "tempfile-bounded",
            }


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)
