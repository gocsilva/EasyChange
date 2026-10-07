from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import os
import re
import shutil
import subprocess
import time

from .process_service import ProcessService


class UbuntuError(RuntimeError):
    pass


@dataclass
class UbuntuSession:
    session_id: str
    distro: str
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    namespace: str = "hml"
    context: str = ""
    environment: str = "hml"
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)


class UbuntuService:
    """Structured WSL/Ubuntu execution plane for EasyChange.

    Commands execute through wsl.exe on the REMOTE_PC and their structured
    results travel back through EasyChange's normal EC2/result-store transport.
    No terminal window, clipboard, OCR or shell focus is required.
    """

    _SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
    _ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    _SENSITIVE_KEY = re.compile(
        r"(password|passwd|secret|token|credential|authorization|private[_-]?key|api[_-]?key|conn(?:ection)?str)",
        re.I,
    )
    _READ_ONLY_UBUNTU = {
        "pwd", "ls", "cat", "head", "tail", "grep", "rg", "which", "whereis",
        "stat", "du", "df", "ps", "pgrep", "uname", "id", "whoami", "date",
        "env", "printenv", "echo", "printf", "hostname", "ip", "ss", "getent", "dig",
        "nslookup", "jq", "yq", "kubectl",
    }
    _KUBECTL_VERBS = {
        "get", "describe", "logs", "explain", "version", "api-resources",
        "api-versions", "cluster-info", "top", "events", "diff", "wait", "auth",
        "config", "rollout", "apply", "create", "delete", "edit", "patch",
        "replace", "scale", "annotate", "label", "taint", "cordon", "uncordon",
        "drain", "exec", "attach", "cp", "port-forward", "proxy", "run",
        "expose", "autoscale", "set",
    }
    _MUTATING_KUBECTL = {
        "apply", "create", "delete", "edit", "patch", "replace", "scale",
        "annotate", "label", "taint", "cordon", "uncordon", "drain", "exec",
        "attach", "cp", "port-forward", "proxy", "run", "expose", "autoscale",
        "set",
    }
    _FORBIDDEN_KUBECTL_FLAGS = {
        "--token", "--password", "--username", "--client-key",
        "--client-certificate", "--certificate-authority", "--server",
        "--kubeconfig", "--context",
    }

    def __init__(self, processes: ProcessService) -> None:
        self.processes = processes
        self._sessions: dict[str, UbuntuSession] = {}

    @staticmethod
    def _decode(raw: bytes) -> str:
        data = bytes(raw or b"")
        if not data:
            return ""
        if b"\x00" in data[:64]:
            try:
                return data.decode("utf-16-le", errors="replace").replace("\ufeff", "")
            except Exception:
                pass
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def _wsl_exe() -> str:
        found = shutil.which("wsl.exe") or shutil.which("wsl")
        if found:
            return found
        system_root = os.environ.get("SystemRoot") or r"C:\Windows"
        candidate = Path(system_root) / "System32" / "wsl.exe"
        if candidate.is_file():
            return str(candidate)
        raise FileNotFoundError("WSL_NOT_AVAILABLE")

    @classmethod
    def _session_id(cls, value: str) -> str:
        normalized = str(value or "default").strip()
        if not cls._SAFE_ID.fullmatch(normalized):
            raise ValueError("INVALID_UBUNTU_SESSION_ID")
        return normalized

    @staticmethod
    def _cwd(value: str) -> str:
        normalized = str(value or "/mnt/c").strip()
        if not normalized.startswith("/") or "\x00" in normalized:
            raise ValueError("UBUNTU_CWD_MUST_BE_ABSOLUTE_LINUX_PATH")
        return normalized

    @classmethod
    def _normalize_env(cls, value: dict[str, Any] | None) -> dict[str, str]:
        output: dict[str, str] = {}
        for key, raw in dict(value or {}).items():
            name = str(key)
            if not cls._ENV_KEY.fullmatch(name):
                raise ValueError(f"INVALID_UBUNTU_ENV_KEY:{name}")
            text = str(raw)
            if "\x00" in text or len(text) > 16384:
                raise ValueError(f"INVALID_UBUNTU_ENV_VALUE:{name}")
            output[name] = text
        return output

    @classmethod
    def _redact_values(cls, env: dict[str, str]) -> list[str]:
        return [
            value for key, value in env.items()
            if value and cls._SENSITIVE_KEY.search(key)
        ]

    def _distros(self) -> list[str]:
        completed = subprocess.run(
            [self._wsl_exe(), "--list", "--quiet"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=5,
            check=False,
        )
        text = self._decode(completed.stdout)
        return [line.strip().replace("\x00", "") for line in text.splitlines() if line.strip()]

    def _choose_distro(self, requested: str = "") -> str:
        wanted = str(requested or "").strip()
        distros = self._distros()
        if wanted:
            match = next((item for item in distros if item.casefold() == wanted.casefold()), None)
            if match is None:
                raise UbuntuError(f"WSL_DISTRO_NOT_FOUND:{wanted}")
            return match
        preferred = next((item for item in distros if item.casefold().startswith("ubuntu")), None)
        if preferred:
            return preferred
        if distros:
            return distros[0]
        raise UbuntuError("WSL_NO_DISTRIBUTIONS")

    def _session(self, session_id: str) -> UbuntuSession:
        key = self._session_id(session_id)
        session = self._sessions.get(key)
        if session is None:
            raise LookupError(f"UBUNTU_SESSION_NOT_FOUND:{key}")
        session.last_used_at = time.time()
        return session

    def _wsl_argv(self, session: UbuntuSession, argv: list[str]) -> list[str]:
        requested = [str(item) for item in (argv or [])]
        if not requested or not requested[0]:
            raise ValueError("UBUNTU_ARGV_REQUIRES_1_TO_64_STRINGS")
        if len(requested) > 64 or any("\x00" in item or len(item) > 8192 for item in requested):
            raise ValueError("UBUNTU_ARGV_REQUIRES_1_TO_64_STRINGS")
        assignments = [f"{key}={value}" for key, value in sorted(session.env.items())]
        return [
            self._wsl_exe(),
            "--distribution", session.distro,
            "--cd", session.cwd,
            "--exec", "env",
            *assignments,
            *requested,
        ]

    def _run_sync(self, session: UbuntuSession, argv: list[str], *, timeout: float = 15.0, max_bytes: int = 65536) -> dict:
        effective = self._wsl_argv(session, argv)
        started = time.monotonic()
        try:
            run_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "shell": False,
                "timeout": max(0.5, min(120.0, float(timeout))),
                "check": False,
            }
            if os.name == "nt":
                run_kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            completed = subprocess.run(effective, **run_kwargs)
            stdout = bytes(completed.stdout or b"")
            stderr = bytes(completed.stderr or b"")
            returncode = int(completed.returncode)
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stdout = bytes(exc.stdout or b"")
            stderr = bytes(exc.stderr or b"")
            returncode = None
            timed_out = True
        budget = max(1024, min(1024 * 1024, int(max_bytes or 65536)))
        return {
            "argv": [str(item) for item in argv],
            "return_code": returncode,
            "timed_out": timed_out,
            "stdout": self._decode(stdout[:budget]),
            "stderr": self._decode(stderr[:budget]),
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
            "stdout_truncated": len(stdout) > budget,
            "stderr_truncated": len(stderr) > budget,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    @classmethod
    def _public_env(cls, env: dict[str, str]) -> dict[str, str]:
        return {
            key: ("[REDACTED]" if cls._SENSITIVE_KEY.search(key) else value)
            for key, value in env.items()
        }

    @staticmethod
    def _environment_matches(context: str, kubeconfig: str, environment: str) -> bool:
        expected = str(environment or "").strip().casefold()
        if not expected:
            return True
        values = (str(context or "").casefold(), str(kubeconfig or "").casefold())
        aliases = {expected}
        if expected == "hml":
            aliases.update({"homolog", "homologacao"})
        return any(alias in value for alias in aliases for value in values)

    def open(
        self,
        session_id: str = "default",
        *,
        distro: str = "",
        cwd: str = "/mnt/c",
        env: dict[str, Any] | None = None,
        kubeconfig: str = "",
        namespace: str = "hml",
        context: str = "",
        environment: str = "hml",
        verify_kubectl: bool = True,
    ) -> dict:
        key = self._session_id(session_id)
        selected = self._choose_distro(distro)
        normalized_env = self._normalize_env(env)
        if kubeconfig:
            normalized_env["KUBECONFIG"] = str(kubeconfig)
        session = UbuntuSession(
            session_id=key,
            distro=selected,
            cwd=self._cwd(cwd),
            env=normalized_env,
            namespace=str(namespace or "hml").strip() or "hml",
            context=str(context or "").strip(),
            environment=str(environment or "").strip(),
        )
        probe = self._run_sync(session, ["/bin/pwd"], timeout=5, max_bytes=8192)
        if probe["return_code"] != 0:
            raise UbuntuError(
                "UBUNTU_SESSION_PROBE_FAILED:" + str(probe.get("stderr") or "")[:300]
            )

        kubectl = {
            "available": None,
            "current_context": None,
            "pinned_context": session.context or None,
            "client_version_ok": None,
            "environment_match": None,
        }
        if verify_kubectl:
            version = self._run_sync(
                session, ["kubectl", "version", "--client", "--output=json"],
                timeout=8, max_bytes=16384,
            )
            kubectl["client_version_ok"] = version["return_code"] == 0
            kubectl["available"] = version["return_code"] != 127
            if version["return_code"] == 0:
                current = self._run_sync(
                    session, ["kubectl", "config", "current-context"],
                    timeout=8, max_bytes=8192,
                )
                if current["return_code"] == 0:
                    current_context = current["stdout"].strip()
                    kubectl["current_context"] = current_context or None
                    if not session.context:
                        session.context = current_context
                if session.context:
                    contexts = self._run_sync(
                        session, ["kubectl", "config", "get-contexts", "-o", "name"],
                        timeout=8, max_bytes=32768,
                    )
                    available = {
                        line.strip()
                        for line in str(contexts.get("stdout") or "").splitlines()
                        if line.strip()
                    }
                    if contexts["return_code"] == 0 and session.context not in available:
                        raise UbuntuError(f"K8S_CONTEXT_NOT_FOUND:{session.context}")

        kubectl["pinned_context"] = session.context or None
        kubectl["environment_match"] = self._environment_matches(
            session.context,
            session.env.get("KUBECONFIG", ""),
            session.environment,
        )
        self._sessions[key] = session
        return {
            "schema": "easychange.ubuntu-session/1",
            "session_id": key,
            "alive": True,
            "distro": selected,
            "cwd": session.cwd,
            "namespace": session.namespace,
            "context": session.context or None,
            "environment": session.environment or None,
            "env": self._public_env(session.env),
            "env_keys": sorted(session.env),
            "kubectl": kubectl,
            "transport": {
                "execution": "WSL_STRUCTURED_EXEC",
                "result": "EC2_RESULT_STORE_CHUNKS",
                "terminal_window_required": False,
                "terminal_focus_required": False,
                "ocr_required": False,
                "clipboard_required": False,
                "shell_interpolation": False,
            },
        }

    def state(self, session_id: str = "default", *, probe: bool = False) -> dict:
        session = self._session(session_id)
        data = {
            "schema": "easychange.ubuntu-session/1",
            "session_id": session.session_id,
            "alive": True,
            "distro": session.distro,
            "cwd": session.cwd,
            "namespace": session.namespace,
            "context": session.context or None,
            "environment": session.environment or None,
            "env": self._public_env(session.env),
            "env_keys": sorted(session.env),
            "created_at": session.created_at,
            "last_used_at": session.last_used_at,
        }
        if probe:
            result = self._run_sync(session, ["/bin/pwd"], timeout=5, max_bytes=8192)
            data["probe"] = result
            data["alive"] = result["return_code"] == 0
        return data

    def close(self, session_id: str = "default") -> dict:
        key = self._session_id(session_id)
        existed = self._sessions.pop(key, None) is not None
        return {"session_id": key, "closed": existed}

    def cd(self, session_id: str, cwd: str) -> dict:
        session = self._session(session_id)
        target = self._cwd(cwd)
        previous = session.cwd
        session.cwd = target
        result = self._run_sync(session, ["/bin/pwd"], timeout=5, max_bytes=8192)
        if result["return_code"] != 0:
            session.cwd = previous
            raise UbuntuError(f"UBUNTU_CWD_NOT_AVAILABLE:{target}")
        return {"session_id": session.session_id, "cwd": target}

    def update_env(self, session_id: str, *, set_values: dict[str, Any] | None = None,
                   unset: list[str] | None = None) -> dict:
        session = self._session(session_id)
        updates = self._normalize_env(set_values)
        for key in list(unset or []):
            name = str(key)
            if not self._ENV_KEY.fullmatch(name):
                raise ValueError(f"INVALID_UBUNTU_ENV_KEY:{name}")
            session.env.pop(name, None)
        session.env.update(updates)
        return {
            "session_id": session.session_id,
            "env": self._public_env(session.env),
            "env_keys": sorted(session.env),
        }

    @classmethod
    def _ubuntu_read_only(cls, argv: list[str]) -> bool:
        if not argv:
            return False
        command = Path(str(argv[0])).name.casefold()
        if command not in cls._READ_ONLY_UBUNTU:
            return False
        lowered = [str(value).casefold() for value in argv[1:]]
        if command == "find" and any(
            value in {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
            for value in lowered
        ):
            return False
        return True

    def _exec_job(
        self,
        session: UbuntuSession,
        argv: list[str],
        *,
        job_id: str,
        wait_ms: int,
        max_bytes: int,
        metadata: dict | None = None,
    ) -> dict:
        effective = self._wsl_argv(session, argv)
        job_metadata = {
            "family": "ubuntu",
            "ubuntu_session_id": session.session_id,
            "distro": session.distro,
            "cwd": session.cwd,
            "context": session.context or None,
            "namespace": session.namespace or None,
            **dict(metadata or {}),
        }
        started = self.processes.job_start(
            effective,
            job_id,
            redact_values=self._redact_values(session.env),
            metadata=job_metadata,
            no_window=True,
        )
        deadline = time.monotonic() + (
            max(0, min(10000, int(wait_ms))) / 1000.0
        )
        status = self.processes.job_status(job_id)
        while not bool(status.get("terminal")) and time.monotonic() < deadline:
            time.sleep(0.02)
            status = self.processes.job_status(job_id)
        out_page = self.processes.job_logs(
            job_id, stream="stdout", cursor=0, max_bytes=max_bytes
        )
        err_page = self.processes.job_logs(
            job_id, stream="stderr", cursor=0, max_bytes=max_bytes
        )
        return {
            "schema": "easychange.ubuntu-exec/1",
            "session_id": session.session_id,
            "job_id": job_id,
            "pid": status.get("pid") or started.get("pid"),
            "argv": [str(item) for item in argv],
            "state": str(status.get("state") or "RUNNING"),
            "terminal": bool(status.get("terminal")),
            "return_code": status.get("returncode"),
            "stdout": out_page.get("text", ""),
            "stderr": err_page.get("text", ""),
            "stdout_page": {
                key: out_page.get(key)
                for key in (
                    "cursor", "next_cursor", "returned_bytes",
                    "total_bytes", "complete",
                )
            },
            "stderr_page": {
                key: err_page.get(key)
                for key in (
                    "cursor", "next_cursor", "returned_bytes",
                    "total_bytes", "complete",
                )
            },
            "cwd": session.cwd,
            "distro": session.distro,
            "context": session.context or None,
            "namespace": session.namespace or None,
            "result_transport": "EC2_RESULT_STORE_CHUNKS",
            "terminal_window_required": False,
        }

    def exec(
        self,
        session_id: str,
        argv: list[str],
        *,
        job_id: str,
        wait_ms: int = 1500,
        max_bytes: int = 65536,
        allow_mutation: bool = False,
        confirm_context: str = "",
    ) -> dict:
        session = self._session(session_id)
        requested = [str(item) for item in (argv or [])]
        if not requested:
            raise ValueError("UBUNTU_ARGV_REQUIRES_1_TO_64_STRINGS")
        if Path(requested[0]).name.casefold() == "kubectl":
            return self.kubectl(
                session_id,
                requested[1:],
                job_id=job_id,
                allow_mutation=allow_mutation,
                confirm_context=confirm_context,
                wait_ms=wait_ms,
                max_bytes=max_bytes,
            )
        read_only = self._ubuntu_read_only(requested)
        if not read_only and not allow_mutation:
            raise UbuntuError("UBUNTU_COMMAND_REQUIRES_ALLOW_MUTATION")
        result = self._exec_job(
            session,
            requested,
            job_id=job_id,
            wait_ms=wait_ms,
            max_bytes=max_bytes,
            metadata={
                "mutation": not read_only,
                "allow_mutation": bool(allow_mutation),
            },
        )
        result["mutation"] = not read_only
        result["allow_mutation"] = bool(allow_mutation)
        return result

    def shell(
        self,
        session_id: str,
        command: str,
        *,
        job_id: str,
        wait_ms: int = 1500,
        max_bytes: int = 65536,
        allow_mutation: bool = False,
    ) -> dict:
        value = str(command or "")
        if not value.strip() or len(value) > 32768 or "\x00" in value:
            raise ValueError("UBUNTU_SHELL_COMMAND_REQUIRED")
        if not allow_mutation:
            raise UbuntuError("UBUNTU_SHELL_REQUIRES_ALLOW_MUTATION")
        session = self._session(session_id)
        result = self._exec_job(
            session,
            ["/bin/bash", "-lc", value],
            job_id=job_id,
            wait_ms=wait_ms,
            max_bytes=max_bytes,
            metadata={"mutation": True, "shell": True},
        )
        result["mutation"] = True
        result["shell_interpolation"] = True
        return result

    def batch(
        self,
        session_id: str,
        commands: list[dict],
        *,
        continue_on_error: bool = False,
        timeout: float = 15.0,
        max_bytes: int = 65536,
    ) -> dict:
        session = self._session(session_id)
        if not isinstance(commands, list) or not 1 <= len(commands) <= 32:
            raise ValueError("UBUNTU_BATCH_REQUIRES_1_TO_32_COMMANDS")
        started = time.monotonic()
        results: list[dict] = []
        failed = 0
        for index, item in enumerate(commands):
            if not isinstance(item, dict):
                raise ValueError(f"UBUNTU_BATCH_COMMAND_OBJECT_REQUIRED:{index}")
            step = time.monotonic()
            try:
                argv = item.get("argv")
                command = item.get("command")
                allow_mutation = bool(item.get("allow_mutation", False))
                confirm_context = str(item.get("confirm_context") or "")
                mutation = False
                if isinstance(argv, list):
                    effective = [str(value) for value in argv]
                    if not effective:
                        raise ValueError(f"UBUNTU_BATCH_COMMAND_REQUIRED:{index}")
                    if Path(effective[0]).name.casefold() == "kubectl":
                        requested = self._validate_kubectl_args(effective[1:])
                        self._guard_secret_read(requested)
                        mutation = self._kubectl_mutating(requested)
                        if mutation:
                            if not allow_mutation:
                                raise UbuntuError(
                                    "KUBECTL_MUTATION_REQUIRES_ALLOW_MUTATION"
                                )
                            if not session.context:
                                raise UbuntuError(
                                    "KUBECTL_MUTATION_REQUIRES_PINNED_CONTEXT"
                                )
                            if confirm_context != session.context:
                                raise UbuntuError(
                                    "KUBECTL_MUTATION_REQUIRES_EXACT_CONTEXT_CONFIRMATION:"
                                    + session.context
                                )
                        effective_args, _ = self._effective_kubectl_args(
                            session,
                            requested,
                            str(item.get("namespace") or ""),
                        )
                        effective = ["kubectl", *effective_args]
                    else:
                        mutation = not self._ubuntu_read_only(effective)
                        if mutation and not allow_mutation:
                            raise UbuntuError(
                                "UBUNTU_COMMAND_REQUIRES_ALLOW_MUTATION"
                            )
                elif isinstance(command, str) and command.strip():
                    if not allow_mutation:
                        raise UbuntuError("UBUNTU_SHELL_REQUIRES_ALLOW_MUTATION")
                    effective = ["/bin/bash", "-lc", command]
                    mutation = True
                else:
                    raise ValueError(f"UBUNTU_BATCH_COMMAND_REQUIRED:{index}")

                result = self._run_sync(
                    session,
                    effective,
                    timeout=float(item.get("timeout") or timeout),
                    max_bytes=int(item.get("max_bytes") or max_bytes),
                )
                ok = (
                    result.get("return_code") == 0
                    and not result.get("timed_out")
                )
                if not ok:
                    failed += 1
                results.append({
                    "index": index,
                    "ok": ok,
                    "mutation": mutation,
                    "duration_ms": int((time.monotonic() - step) * 1000),
                    **result,
                })
                if not ok and not continue_on_error:
                    break
            except Exception as exc:
                failed += 1
                results.append({
                    "index": index,
                    "ok": False,
                    "duration_ms": int((time.monotonic() - step) * 1000),
                    "error": f"{type(exc).__name__}:{str(exc)[:1000]}",
                })
                if not continue_on_error:
                    break
        return {
            "schema": "easychange.ubuntu-batch/1",
            "session_id": session.session_id,
            "ok": failed == 0,
            "count": len(results),
            "failed": failed,
            "results": results,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "context": session.context or None,
            "namespace": session.namespace or None,
            "one_ec2_round_trip": True,
            "result_transport": "EC2_RESULT_STORE_CHUNKS",
        }

    def _job(self, session_id: str, job_id: str) -> dict:
        session = self._session(session_id)
        status = self.processes.job_status(job_id)
        metadata = dict(status.get("metadata") or {})
        if metadata.get("family") != "ubuntu" or str(metadata.get("ubuntu_session_id") or "") != session.session_id:
            raise UbuntuError("UBUNTU_JOB_SESSION_MISMATCH")
        return status

    def job_status(self, session_id: str, job_id: str) -> dict:
        return self._job(session_id, job_id)

    def job_logs(self, session_id: str, job_id: str, *, stream: str = "stdout",
                 cursor: int = 0, max_bytes: int = 65536) -> dict:
        self._job(session_id, job_id)
        return self.processes.job_logs(job_id, stream=stream, cursor=cursor, max_bytes=max_bytes)

    def job_cancel(self, session_id: str, job_id: str) -> dict:
        self._job(session_id, job_id)
        return self.processes.job_cancel(job_id)

    @classmethod
    def _validate_kubectl_args(cls, args: list[str]) -> list[str]:
        requested = [str(item) for item in (args or [])]
        if requested and Path(requested[0]).name.casefold() == "kubectl":
            requested = requested[1:]
        if not requested or len(requested) > 64:
            raise ValueError("KUBECTL_ARGS_REQUIRES_1_TO_64_STRINGS")
        lowered = [item.casefold() for item in requested]
        for flag in cls._FORBIDDEN_KUBECTL_FLAGS:
            for value in lowered:
                if value == flag or value.startswith(flag + "="):
                    raise UbuntuError(
                        "KUBECTL_CONNECTION_OR_CONTEXT_OVERRIDE_FORBIDDEN:" + flag
                    )
        return requested

    @classmethod
    def _kubectl_verb(cls, args: list[str]) -> tuple[str, str]:
        lowered = [str(item).casefold() for item in args]
        verb = next((item for item in lowered if item in cls._KUBECTL_VERBS), "")
        if not verb:
            return "", ""
        index = lowered.index(verb)
        subverb = ""
        for item in lowered[index + 1:]:
            if item and not item.startswith("-"):
                subverb = item
                break
        return verb, subverb

    @classmethod
    def _kubectl_mutating(cls, args: list[str]) -> bool:
        verb, subverb = cls._kubectl_verb(args)
        if verb in cls._MUTATING_KUBECTL:
            return True
        if verb == "rollout":
            return subverb not in {"status", "history"}
        if verb == "config":
            return subverb not in {"current-context", "get-contexts", "view"}
        if verb == "auth":
            return subverb != "can-i"
        return False

    @classmethod
    def _guard_secret_read(cls, args: list[str]) -> None:
        verb, _ = cls._kubectl_verb(args)
        if verb != "get":
            return
        lowered = [str(item).casefold() for item in args]
        verb_index = next(
            (i for i, value in enumerate(lowered) if value == "get"),
            -1,
        )
        if verb_index < 0:
            return
        resource = next(
            (
                value
                for value in lowered[verb_index + 1:]
                if value and not value.startswith("-")
            ),
            "",
        )
        resource = resource.split("/", 1)[0]
        if resource not in {"secret", "secrets"}:
            return
        for index, value in enumerate(lowered):
            output = ""
            if value in {"-o", "--output"} and index + 1 < len(lowered):
                output = lowered[index + 1]
            elif value.startswith(("-o=", "--output=")):
                output = value.split("=", 1)[1]
            if output.startswith(
                ("json", "yaml", "jsonpath", "go-template", "template", "custom-columns")
            ):
                raise UbuntuError("K8S_SECRET_VALUE_READ_FORBIDDEN")

    def _effective_kubectl_args(
        self,
        session: UbuntuSession,
        args: list[str],
        namespace: str = "",
    ) -> tuple[list[str], str]:
        requested = self._validate_kubectl_args(args)
        ns = str(namespace or session.namespace or "").strip()
        effective: list[str] = []
        if session.context:
            effective.extend(["--context", session.context])
        has_namespace = any(
            value == "-n"
            or value == "--namespace"
            or value.startswith("--namespace=")
            for value in requested
        )
        if ns and not has_namespace:
            effective.extend(["--namespace", ns])
        effective.extend(requested)
        return effective, ns

    def kubectl(
        self,
        session_id: str,
        args: list[str],
        *,
        job_id: str,
        namespace: str = "",
        allow_mutation: bool = False,
        confirm_context: str = "",
        wait_ms: int = 1800,
        max_bytes: int = 65536,
    ) -> dict:
        session = self._session(session_id)
        requested = self._validate_kubectl_args(args)
        self._guard_secret_read(requested)
        mutating = self._kubectl_mutating(requested)
        if mutating:
            verb, subverb = self._kubectl_verb(requested)
            if not allow_mutation:
                raise UbuntuError(
                    "KUBECTL_MUTATION_REQUIRES_ALLOW_MUTATION:"
                    + ":".join(part for part in (verb, subverb) if part)
                )
            if not session.context:
                raise UbuntuError("KUBECTL_MUTATION_REQUIRES_PINNED_CONTEXT")
            if str(confirm_context or "") != session.context:
                raise UbuntuError(
                    "KUBECTL_MUTATION_REQUIRES_EXACT_CONTEXT_CONFIRMATION:"
                    + session.context
                )
        effective, ns = self._effective_kubectl_args(session, requested, namespace)
        result = self._exec_job(
            session,
            ["kubectl", *effective],
            job_id=job_id,
            wait_ms=wait_ms,
            max_bytes=max_bytes,
            metadata={
                "family": "kubernetes",
                "mutation": mutating,
                "allow_mutation": bool(allow_mutation),
                "confirmed_context": str(confirm_context or "") or None,
            },
        )
        result["kubectl"] = {
            "args": requested,
            "effective_args": effective,
            "namespace": ns or None,
            "context": session.context or None,
            "mutation": mutating,
            "allow_mutation": bool(allow_mutation),
            "context_confirmed": bool(
                not mutating or str(confirm_context or "") == session.context
            ),
        }
        return result

    def context(self, session_id: str) -> dict:
        session = self._session(session_id)
        current = self._run_sync(
            session,
            ["kubectl", "config", "current-context"],
            timeout=8,
            max_bytes=8192,
        )
        cluster_argv = ["kubectl"]
        if session.context:
            cluster_argv += ["--context", session.context]
        cluster_argv += [
            "config", "view", "--minify",
            "-o", "jsonpath={.clusters[0].name}",
        ]
        cluster = self._run_sync(
            session,
            cluster_argv,
            timeout=8,
            max_bytes=8192,
        )
        return {
            "schema": "easychange.k8s-context/1",
            "session_id": session.session_id,
            "namespace": session.namespace,
            "current_context": (
                current["stdout"].strip()
                if current["return_code"] == 0 else None
            ),
            "pinned_context": session.context or None,
            "cluster": (
                cluster["stdout"].strip()
                if cluster["return_code"] == 0 else None
            ),
            "environment": session.environment or None,
            "environment_match": self._environment_matches(
                session.context,
                session.env.get("KUBECONFIG", ""),
                session.environment,
            ),
            "kubeconfig": session.env.get("KUBECONFIG"),
            "global_context_changed": False,
            "ok": current["return_code"] == 0,
            "stderr": current["stderr"],
        }

