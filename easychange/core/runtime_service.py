from __future__ import annotations
import html

import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .process_service import ProcessService
from .workspace import Workspace

_SKIP = {".git", ".easychange", "bin", "obj", "node_modules", ".venv", "venv", "dist", "build"}
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_ENV_RE = re.compile(r"(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|CONNECTION_?STRING)", re.I)
_ENV_REF_PREFIXES = {"PROCESS_ENV", "REMOTE_ENV", "ENV", "MCP_HOST_ENV"}


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return value[:80] or "evidence"


class _SameOriginRuntimeRedirect(urllib.request.HTTPRedirectHandler):
    """Keep health/Swagger probes on the exact original HTTP origin."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            old=urlsplit(req.full_url)
            new=urlsplit(newurl)
            def origin(parts):
                port=parts.port
                if port is None:
                    port=443 if parts.scheme=="https" else 80
                return parts.scheme, parts.hostname, port
            if origin(old)!=origin(new) or new.username or new.password:
                return None
        except ValueError:
            return None
        return super().redirect_request(req,fp,code,msg,headers,newurl)


class ProjectRuntimeService:
    """Deterministic project runtime/test/evidence helper for Machine Mode."""

    def __init__(
        self,
        workspace: Workspace,
        processes: ProcessService,
        external_env_provider=None,
    ) -> None:
        self.workspace = workspace
        self.processes = processes
        self.external_env_provider = external_env_provider
        self.evidence_root = workspace.root_path / ".easychange" / "evidence"
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        self.config_path = workspace.root_path / ".easychange" / "runtime_profiles.json"

    @staticmethod
    def _argv(value) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list) or not value or len(value) > 64:
            raise ValueError("Runtime argv must be a non-empty list with at most 64 items")
        result = [str(item) for item in value]
        if any(not item or len(item) > 2048 for item in result):
            raise ValueError("Invalid runtime argv item")
        return result

    @staticmethod
    def _env_values(value) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict) or len(value) > 64:
            raise ValueError("Runtime env must be an object with at most 64 entries")
        result: dict[str, str] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key or "").strip()
            if not _ENV_NAME_RE.fullmatch(key):
                raise ValueError(f"INVALID_ENV_NAME:{key}")
            if _SENSITIVE_ENV_RE.search(key):
                raise ValueError(f"SECRET_ENV_VALUE_NOT_ALLOWED_USE_ENV_REF:{key}")
            text = str(raw_value)
            if len(text) > 4096:
                raise ValueError(f"ENV_VALUE_TOO_LONG:{key}")
            result[key] = text
        return result

    @staticmethod
    def _env_refs(value) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict) or len(value) > 64:
            raise ValueError("Runtime env_refs must be an object with at most 64 entries")
        result: dict[str, str] = {}
        for raw_key, raw_ref in value.items():
            key = str(raw_key or "").strip()
            if not _ENV_NAME_RE.fullmatch(key):
                raise ValueError(f"INVALID_ENV_NAME:{key}")
            ref = str(raw_ref or "").strip()
            if ":" not in ref:
                raise ValueError(f"INVALID_ENV_REF:{key}")
            prefix, source = ref.split(":", 1)
            prefix = prefix.strip().upper()
            source = source.strip()
            if prefix not in _ENV_REF_PREFIXES or not _ENV_NAME_RE.fullmatch(source):
                raise ValueError(f"INVALID_ENV_REF:{key}")
            result[key] = f"{prefix}:{source}"
        return result

    @staticmethod
    def _env_allowlist(value) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 128:
            raise ValueError("Runtime env_allowlist must be a list with at most 128 entries")
        result = []
        for raw in value:
            key = str(raw or "").strip()
            if not _ENV_NAME_RE.fullmatch(key):
                raise ValueError(f"INVALID_ENV_ALLOWLIST_NAME:{key}")
            if key not in result:
                result.append(key)
        return result

    def resolve_environment(
        self,
        profile: dict[str, Any],
        *,
        external_env: dict[str, str] | None = None,
        require_all: bool = True,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        inherit_env = bool(profile.get("inherit_env", True))
        allowlist = self._env_allowlist(profile.get("env_allowlist"))
        values = self._env_values(profile.get("env"))
        refs = self._env_refs(profile.get("env_refs"))

        env: dict[str, str] = dict(os.environ) if inherit_env else {}
        host_env = external_env
        if host_env is None and self.external_env_provider is not None:
            try:
                supplied = self.external_env_provider()
                host_env = dict(supplied or {})
            except Exception:
                host_env = {}
        host_env = dict(host_env or {})
        for key in allowlist:
            if key in os.environ:
                env[key] = os.environ[key]
        env.update(values)

        resolved = list(values)
        unresolved: list[str] = []
        for key, ref in refs.items():
            prefix, source = ref.split(":", 1)
            if prefix in {"PROCESS_ENV", "REMOTE_ENV", "ENV"}:
                source_value = os.environ.get(source)
            else:
                source_value = host_env.get(source)
            if source_value is None:
                unresolved.append(ref)
                continue
            env[key] = str(source_value)
            if key not in resolved:
                resolved.append(key)

        if require_all and unresolved:
            raise RuntimeError("RUNTIME_ENV_REF_UNRESOLVED:" + ",".join(unresolved))
        metadata = {
            "inherit_env": inherit_env,
            "env_allowlist": allowlist,
            "resolved_env_keys": sorted(resolved),
            "unresolved_env_refs": list(unresolved),
            "secret_values_returned": False,
        }
        return env, metadata

    @staticmethod
    def _redaction_values(profile: dict[str, Any], resolved_env: dict[str, str]) -> list[str]:
        refs = profile.get("env_refs") if isinstance(profile.get("env_refs"), dict) else {}
        values = []
        for key in refs:
            value = resolved_env.get(str(key))
            if value and value not in values:
                values.append(value)
        return values

    def environment_status(self, profile_id: str = "") -> dict[str, Any]:
        profile = self.profile(profile_id)
        _env, metadata = self.resolve_environment(profile, require_all=False)
        return {
            "profile_id": profile.get("id"),
            **metadata,
            "env_keys": sorted((profile.get("env") or {}).keys()),
            "env_refs": dict(profile.get("env_refs") or {}),
        }


    def environment_resolution_status(
        self,
        profile_id: str = "",
        *,
        env_key: str = "",
        ref_name: str = "",
    ) -> dict[str, Any]:
        """Return compact, secret-free resolution state for one runtime profile."""
        profile = self.profile(profile_id)
        _env, metadata = self.resolve_environment(profile, require_all=False)
        refs = self._env_refs(profile.get("env_refs"))
        resolved_keys = {
            str(item) for item in (metadata.get("resolved_env_keys") or [])
        }
        unresolved_refs = {
            str(item) for item in (metadata.get("unresolved_env_refs") or [])
        }
        unresolved_keys = sorted(
            key for key, ref in refs.items() if ref in unresolved_refs
        )
        resolved_ref_keys = sorted(
            key for key, ref in refs.items()
            if ref not in unresolved_refs and key in resolved_keys
        )

        key_filter = str(env_key or "").strip()
        ref_filter = str(ref_name or "").strip()
        selected = None
        if key_filter:
            selected_ref = refs.get(key_filter)
            selected = {
                "env_key": key_filter,
                "configured": bool(
                    key_filter in refs
                    or key_filter in (profile.get("env") or {})
                ),
                "uses_ref": selected_ref is not None,
                "ref": selected_ref,
                "resolved": key_filter in resolved_keys,
                "unresolved": (
                    bool(selected_ref)
                    and selected_ref in unresolved_refs
                ),
            }
        elif ref_filter:
            matching_keys = sorted(
                key for key, ref in refs.items()
                if ref == ref_filter
                or ref.split(":", 1)[-1] == ref_filter
            )
            selected = {
                "ref": ref_filter,
                "configured": bool(matching_keys),
                "env_keys": matching_keys,
                "resolved": bool(matching_keys)
                and all(key in resolved_keys for key in matching_keys),
                "unresolved": any(
                    refs[key] in unresolved_refs for key in matching_keys
                ),
            }

        return {
            "profile_id": profile.get("id"),
            "runnable": bool(profile.get("runnable")),
            "all_refs_resolved": not unresolved_keys,
            "resolved_ref_keys": resolved_ref_keys,
            "unresolved_ref_keys": unresolved_keys,
            "resolved_ref_count": len(resolved_ref_keys),
            "unresolved_ref_count": len(unresolved_keys),
            "configured_ref_count": len(refs),
            "selected": selected,
            "secret_values_returned": False,
            "compact": True,
        }

    def _configured_profiles(self) -> list[dict[str, Any]]:
        if not self.config_path.exists():
            return []
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        rows = value.get("profiles") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            return []
        output = []
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("id") or "").strip():
                continue
            try:
                run_argv = self._argv(row.get("run_argv"))
                test_argv = self._argv(row.get("test_argv"))
                build_argv = self._argv(row.get("build_argv"))
                env_values = self._env_values(row.get("env"))
                env_refs = self._env_refs(row.get("env_refs"))
                env_allowlist = self._env_allowlist(row.get("env_allowlist"))
            except ValueError:
                continue
            profile_row = {
                "id": str(row["id"]).strip(),
                "ecosystem": str(row.get("ecosystem") or "custom"),
                "kind": str(row.get("kind") or "custom"),
                "project": str(row.get("project") or "runtime_profiles.json"),
                "runnable": bool(run_argv),
                "run_argv": run_argv,
                "test_argv": test_argv,
                "build_argv": build_argv,
                "urls": [str(item) for item in (row.get("urls") or []) if str(item).startswith(("http://", "https://"))][:16],
                "swagger_candidates": [str(item) for item in (row.get("swagger_candidates") or []) if str(item).startswith(("http://", "https://"))][:32],
                "configured": True,
                "env": env_values,
                "env_refs": env_refs,
                "inherit_env": bool(row.get("inherit_env", True)),
                "env_allowlist": env_allowlist,
            }
            try:
                _resolved, env_meta = self.resolve_environment(profile_row, require_all=False)
            except Exception:
                env_meta = {"resolved_env_keys": sorted(env_values), "unresolved_env_refs": list(env_refs.values())}
            profile_row["resolved_env_keys"] = env_meta.get("resolved_env_keys", [])
            profile_row["unresolved_env_refs"] = env_meta.get("unresolved_env_refs", [])
            output.append(profile_row)
        return output

    def configure_profile(
        self,
        profile_id: str,
        *,
        kind: str = "custom",
        run_argv: list[str] | None = None,
        test_argv: list[str] | None = None,
        build_argv: list[str] | None = None,
        urls: list[str] | None = None,
        env: dict[str, str] | None = None,
        env_refs: dict[str, str] | None = None,
        inherit_env: bool = True,
        env_allowlist: list[str] | None = None,
    ) -> dict:
        profile_id = str(profile_id or "").strip()
        if not profile_id:
            raise ValueError("PROFILE_ID_REQUIRED")
        run = self._argv(run_argv)
        test = self._argv(test_argv)
        build = self._argv(build_argv)
        safe_urls = [str(item) for item in (urls or []) if str(item).startswith(("http://", "https://"))][:16]
        env_values = self._env_values(env)
        safe_refs = self._env_refs(env_refs)
        safe_allowlist = self._env_allowlist(env_allowlist)
        existing = self._configured_profiles()
        row = {
            "id": profile_id, "ecosystem": "custom", "kind": str(kind or "custom"),
            "project": "runtime_profiles.json", "runnable": bool(run),
            "run_argv": run, "test_argv": test, "build_argv": build, "urls": safe_urls,
            "swagger_candidates": self._swagger_candidates(safe_urls),
            "configured": True,
            "env": env_values,
            "env_refs": safe_refs,
            "inherit_env": bool(inherit_env),
            "env_allowlist": safe_allowlist,
        }
        by_id = {item["id"]: item for item in existing}
        by_id[profile_id] = row
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({"profiles": list(by_id.values())}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _resolved, env_meta = self.resolve_environment(row, require_all=False)
        public_row = {**row, **env_meta}
        return {"profile": public_row, "config": ".easychange/runtime_profiles.json"}

    def profiles(self) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = self._configured_profiles()
        configured_ids = {item["id"] for item in profiles}
        profiles.extend(item for item in self._dotnet_profiles() if item["id"] not in configured_ids)
        profiles.extend(self._node_profiles())
        profiles.extend(self._python_profiles())
        profiles.extend(self._other_profiles())
        return profiles

    def _dotnet_profiles(self) -> list[dict[str, Any]]:
        output = []
        projects = []
        for project in sorted(self.workspace.root_path.rglob("*.csproj")):
            if any(part in _SKIP for part in project.relative_to(self.workspace.root_path).parts):
                continue
            projects.append(project)
            try:
                xml = ET.parse(project).getroot()
            except (OSError, ET.ParseError):
                continue
            sdk = (xml.attrib.get("Sdk") or "").casefold()
            values: dict[str, str] = {}
            packages = []
            for element in xml.iter():
                tag = element.tag.split("}")[-1]
                text = (element.text or "").strip()
                if text and tag in {"OutputType", "TargetFramework", "TargetFrameworks", "IsTestProject", "AzureFunctionsVersion"}:
                    values[tag] = text
                if tag == "PackageReference":
                    packages.append(str(element.attrib.get("Include") or "").casefold())
            project_rel = _rel(self.workspace.root_path, project)
            package_text = " ".join(packages)
            is_test = values.get("IsTestProject", "").casefold() == "true" or any(
                token in package_text for token in ("microsoft.net.test.sdk", "xunit", "nunit", "mstest")
            )
            source_hint = self._source_hint(project.parent)
            kind = "dotnet-library"
            if is_test:
                kind = "dotnet-test"
            elif "microsoft.net.sdk.web" in sdk or "webapplication." in source_hint or "mapcontrollers(" in source_hint:
                kind = "dotnet-api"
            elif "microsoft.azure.functions" in package_text or values.get("AzureFunctionsVersion"):
                kind = "dotnet-function"
            elif any(token in package_text + "\n" + source_hint for token in ("quartz", "hangfire", "cronos", "cronexpression")):
                kind = "dotnet-scheduler"
            elif "backgroundservice" in source_hint or "ihostedservice" in source_hint:
                kind = "dotnet-worker"
            elif values.get("OutputType", "").casefold() in {"exe", "winexe"}:
                kind = "dotnet-console"

            restored = (project.parent / "obj" / "project.assets.json").exists()
            run_argv = None
            if not is_test and kind != "dotnet-library":
                run_argv = ["dotnet", "run"]
                if restored:
                    run_argv.append("--no-restore")
                run_argv += ["--project", project_rel]
            test_argv = None
            if is_test:
                test_argv = ["dotnet", "test", project_rel]
                if restored:
                    test_argv.append("--no-restore")
            urls = self._dotnet_urls(project)
            output.append({
                "id": f"dotnet:{project_rel}",
                "ecosystem": "dotnet",
                "kind": kind,
                "project": project_rel,
                "target_framework": values.get("TargetFramework") or values.get("TargetFrameworks"),
                "restored": restored,
                "runnable": bool(run_argv),
                "run_argv": run_argv,
                "test_argv": test_argv,
                "urls": urls,
                "swagger_candidates": self._swagger_candidates(urls),
            })
        solutions = [
            path for path in sorted(
                [
                    *self.workspace.root_path.rglob("*.sln"),
                    *self.workspace.root_path.rglob("*.slnx"),
                ],
                key=lambda value: (
                    len(value.relative_to(self.workspace.root_path).parts),
                    value.relative_to(self.workspace.root_path).as_posix().casefold(),
                ),
            )
            if not any(
                part in _SKIP
                for part in path.relative_to(self.workspace.root_path).parts
            )
        ]
        solution = solutions[0] if len(solutions) == 1 else None
        all_restored = bool(projects) and all((path.parent / "obj" / "project.assets.json").exists() for path in projects)
        if output and solution:
            workspace_test = ["dotnet", "test", _rel(self.workspace.root_path, solution)]
            if all_restored:
                workspace_test.append("--no-restore")
            for item in output:
                if not item.get("test_argv"):
                    item["workspace_test_argv"] = list(workspace_test)
        return output

    def _source_hint(self, root: Path) -> str:
        pieces = []
        scanned = 0
        try:
            for path in root.rglob("*.cs"):
                if any(part in _SKIP for part in path.relative_to(root).parts):
                    continue
                try:
                    pieces.append(path.read_text(encoding="utf-8-sig", errors="ignore")[:16000].casefold())
                    scanned += 1
                    if scanned >= 12:
                        break
                except OSError:
                    continue
        except OSError:
            pass
        return "\n".join(pieces)

    def _dotnet_urls(self, project: Path) -> list[str]:
        settings = project.parent / "Properties" / "launchSettings.json"
        if not settings.exists():
            return []
        try:
            value = json.loads(settings.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        urls = []
        for profile in (value.get("profiles") or {}).values():
            if not isinstance(profile, dict):
                continue
            for url in str(profile.get("applicationUrl") or "").split(";"):
                url = url.strip()
                if url.startswith(("http://", "https://")) and url not in urls:
                    urls.append(url.rstrip("/"))
        return urls

    @staticmethod
    def _swagger_candidates(urls: list[str]) -> list[str]:
        result = []
        for base in urls:
            for suffix in ("/swagger/index.html", "/swagger/v1/swagger.json", "/openapi/v1.json", "/openapi.json"):
                value = base.rstrip("/") + suffix
                if value not in result:
                    result.append(value)
        return result

    def _node_profiles(self) -> list[dict[str, Any]]:
        package = self.workspace.root_path / "package.json"
        if not package.exists():
            return []
        try:
            value = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        scripts = value.get("scripts") or {}
        run_script = "dev" if "dev" in scripts else "start" if "start" in scripts else None
        return [{
            "id": "node:package.json", "ecosystem": "node", "kind": "node-app", "project": "package.json",
            "runnable": bool(run_script), "run_argv": ["npm", "run", run_script] if run_script else None,
            "test_argv": ["npm", "test", "--", "--run"] if "test" in scripts else None,
            "urls": [], "swagger_candidates": [],
        }]

    def _python_profiles(self) -> list[dict[str, Any]]:
        root = self.workspace.root_path
        if not ((root / "pyproject.toml").exists() or (root / "requirements.txt").exists()):
            return []
        entry = next((name for name in ("main.py", "app.py", "__main__.py") if (root / name).exists()), None)
        return [{
            "id": "python:workspace", "ecosystem": "python", "kind": "python-app",
            "project": entry or "pyproject.toml", "runnable": bool(entry),
            "run_argv": ["python", entry] if entry else None,
            "test_argv": ["python", "-m", "pytest", "-q"],
            "urls": [], "swagger_candidates": [],
        }]

    def _other_profiles(self) -> list[dict[str, Any]]:
        root = self.workspace.root_path
        output = []
        if (root / "go.mod").exists():
            output.append({"id": "go:workspace", "ecosystem": "go", "kind": "go-app", "project": "go.mod",
                           "runnable": True, "run_argv": ["go", "run", "."], "test_argv": ["go", "test", "./..."],
                           "urls": [], "swagger_candidates": []})
        if (root / "Cargo.toml").exists():
            output.append({"id": "rust:workspace", "ecosystem": "rust", "kind": "rust-app", "project": "Cargo.toml",
                           "runnable": True, "run_argv": ["cargo", "run"], "test_argv": ["cargo", "test"],
                           "urls": [], "swagger_candidates": []})
        if (root / "pom.xml").exists():
            output.append({"id": "java:maven", "ecosystem": "java", "kind": "java-maven", "project": "pom.xml",
                           "runnable": True, "run_argv": ["mvn", "spring-boot:run"], "test_argv": ["mvn", "test"],
                           "urls": [], "swagger_candidates": []})
        gradlew = "gradlew.bat" if os.name == "nt" and (root / "gradlew.bat").exists() else "gradlew" if (root / "gradlew").exists() else None
        if gradlew:
            output.append({"id": "java:gradle", "ecosystem": "java", "kind": "java-gradle", "project": gradlew,
                           "runnable": True, "run_argv": [str(root / gradlew), "bootRun"], "test_argv": [str(root / gradlew), "test"],
                           "urls": [], "swagger_candidates": []})
        return output

    def profile(self, profile_id: str = "", *, profiles: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Select a runtime profile, optionally reusing an already-detected list."""
        available = profiles if profiles is not None else self.profiles()
        if profile_id:
            for item in available:
                if item.get("id") == profile_id or item.get("project") == profile_id:
                    return item
            raise LookupError(f"Runtime profile not found: {profile_id}")
        runnable = [item for item in available if item.get("runnable")]
        api = next((item for item in runnable if "api" in str(item.get("kind"))), None)
        if api:
            return api
        if runnable:
            return runnable[0]
        if available:
            return available[0]
        raise RuntimeError("No runtime profile detected")

    def start(
        self,
        process_id: str,
        profile_id: str = "",
        *,
        external_env: dict[str, str] | None = None,
    ) -> dict:
        profile = self.profile(profile_id)
        argv = profile.get("run_argv")
        if not argv:
            raise RuntimeError(f"Profile is not runnable: {profile.get('id')}")
        process_env, env_meta = self.resolve_environment(
            profile,
            external_env=external_env,
        )
        result = self.processes.start(
            list(argv),
            process_id,
            env=process_env,
            redact_values=self._redaction_values(profile, process_env),
        )
        return {**result, "profile": profile, "environment": env_meta}

    @staticmethod
    def _test_summary(results: list[dict]) -> dict:
        tests_passed = tests_failed = tests_skipped = 0
        build_errors = 0
        return_codes = []
        for item in results:
            try:
                return_codes.append(int(item.get("returncode") or 0))
            except (TypeError, ValueError):
                return_codes.append(1)
            text = "\n".join([
                str(item.get("stdout") or ""),
                str(item.get("stderr") or ""),
                str(item.get("diagnostics") or ""),
            ])
            # dotnet/vstest labels and pytest-style summaries.
            labels = {
                "passed": re.findall(r"(?i)\bPassed\s*:\s*(\d+)", text),
                "failed": re.findall(r"(?i)\bFailed\s*:\s*(\d+)", text),
                "skipped": re.findall(r"(?i)\bSkipped\s*:\s*(\d+)", text),
            }
            if labels["passed"]:
                tests_passed += int(labels["passed"][-1])
            else:
                pytest_passed = re.findall(r"(?i)\b(\d+)\s+passed\b", text)
                tests_passed += int(pytest_passed[-1]) if pytest_passed else 0
            if labels["failed"]:
                tests_failed += int(labels["failed"][-1])
            else:
                pytest_failed = re.findall(r"(?i)\b(\d+)\s+failed\b", text)
                tests_failed += int(pytest_failed[-1]) if pytest_failed else 0
            if labels["skipped"]:
                tests_skipped += int(labels["skipped"][-1])
            else:
                pytest_skipped = re.findall(r"(?i)\b(\d+)\s+skipped\b", text)
                tests_skipped += int(pytest_skipped[-1]) if pytest_skipped else 0
            build_errors += len(re.findall(r"(?im)^.*\berror\s+[A-Z]{2,}\d+\s*:", text))
        return_code = next((code for code in return_codes if code != 0), 0)
        return {
            "finished": True,
            "return_code": return_code,
            "tests_passed": tests_passed,
            "tests_failed": tests_failed,
            "tests_skipped": tests_skipped,
            "build_errors_count": build_errors,
            "test_failures_count": tests_failed,
        }

    def _adapter_command(self, mode: str) -> list[str] | None:
        from easychange.adapters.dotnet import DotnetAdapter
        from easychange.adapters.node import NodeAdapter
        from easychange.adapters.python import PythonAdapter

        for adapter in (DotnetAdapter(), NodeAdapter(), PythonAdapter()):
            if not adapter.detect(self.workspace):
                continue
            command = adapter.build(self.workspace) if mode == "build" else adapter.test(self.workspace)
            if command:
                return list(command)
        return None

    def _runtime_job_target(
        self,
        mode: str,
        profile_id: str = "",
    ) -> tuple[list[str], dict[str, Any] | None]:
        if mode not in {"build", "test"}:
            raise ValueError("RUNTIME_JOB_MODE_MUST_BE_BUILD_OR_TEST")
        profiles = self.profiles()
        target: dict[str, Any] | None = None
        argv: list[str] | None = None

        if profile_id:
            target = self.profile(profile_id, profiles=profiles)
            key = "build_argv" if mode == "build" else "test_argv"
            candidate = target.get(key)
            if mode == "test" and not candidate:
                candidate = target.get("workspace_test_argv")
            if candidate:
                argv = list(candidate)
            elif mode == "build":
                ecosystem = str(target.get("ecosystem") or "").casefold()
                project = str(target.get("project") or "")
                if ecosystem == "dotnet" and project and project != "runtime_profiles.json":
                    argv = ["dotnet", "build", project]
                    if "--no-restore" in list(target.get("test_argv") or []):
                        argv.append("--no-restore")
        else:
            if mode == "test":
                target = next((item for item in profiles if item.get("workspace_test_argv")), None)
                if target:
                    argv = list(target["workspace_test_argv"])
                if argv is None:
                    target = next((item for item in profiles if item.get("test_argv")), None)
                    if target:
                        argv = list(target["test_argv"])
            else:
                target = next((item for item in profiles if item.get("build_argv")), None)
                if target:
                    argv = list(target["build_argv"])

        if argv is None:
            argv = self._adapter_command(mode)
        if not argv:
            raise RuntimeError(f"No {mode} command detected for this workspace")
        return argv, target

    def _start_runtime_job(
        self,
        mode: str,
        job_id: str,
        profile_id: str = "",
        *,
        external_env: dict[str, str] | None = None,
    ) -> dict:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("JOB_ID_REQUIRED")
        argv, profile = self._runtime_job_target(mode, profile_id)
        if profile is not None:
            process_env, env_meta = self.resolve_environment(
                profile,
                external_env=external_env,
            )
            redact_values = self._redaction_values(profile, process_env)
            profile_name = str(profile.get("id") or profile.get("project") or "")
        else:
            process_env = None
            env_meta = {
                "inherit_env": True,
                "env_allowlist": [],
                "resolved_env_keys": [],
                "unresolved_env_refs": [],
                "secret_values_returned": False,
            }
            redact_values = []
            profile_name = ""
        started = self.processes.job_start(
            argv,
            job_id,
            env=process_env,
            redact_values=redact_values,
            metadata={
                "job_kind": mode,
                "profile_id": profile_name or None,
            },
        )
        return {
            **started,
            "job_kind": mode,
            "profile_id": profile_name or None,
            "environment": env_meta,
        }

    def start_test_job(
        self,
        job_id: str,
        profile_id: str = "",
        *,
        external_env: dict[str, str] | None = None,
    ) -> dict:
        return self._start_runtime_job(
            "test",
            job_id,
            profile_id,
            external_env=external_env,
        )

    def start_build_job(
        self,
        job_id: str,
        profile_id: str = "",
        *,
        external_env: dict[str, str] | None = None,
    ) -> dict:
        return self._start_runtime_job(
            "build",
            job_id,
            profile_id,
            external_env=external_env,
        )

    def test_job_result(self, job_id: str) -> dict:
        result = self.processes.job_result(job_id)
        if not result.get("terminal"):
            return {
                **result,
                "finished": False,
                "passed": None,
                "tests_passed": 0,
                "tests_failed": 0,
                "tests_skipped": 0,
                "build_errors_count": 0,
                "test_failures_count": 0,
            }
        summary = self._test_summary([result])
        return {
            **result,
            **summary,
            "passed": bool(result.get("returncode") == 0),
        }

    def build_job_result(
        self,
        job_id: str,
        *,
        retry_owned_lock: bool = False,
    ) -> dict:
        result = self.processes.job_result(job_id)
        terminal = bool(result.get("terminal"))
        passed = bool(result.get("returncode") == 0) if terminal else None
        combined = "\n".join([
            str(result.get("stdout") or ""),
            str(result.get("stderr") or ""),
            str(result.get("diagnostics") or ""),
        ])
        lock = (
            self.processes.diagnose_file_locks(result)
            if terminal and passed is False
            else {
                "detected": False,
                "codes": [],
                "files": [],
                "blockers": [],
                "owned_blockers": [],
                "foreign_blockers": [],
                "safe_auto_stop": False,
                "unresolved_blocker": False,
            }
        )
        response = {
            **result,
            "finished": terminal,
            "passed": passed,
            "return_code": result.get("returncode"),
            "build_errors_count": len(
                re.findall(
                    r"(?im)^.*\berror\s+[A-Z]{2,}\d+\s*:",
                    combined,
                )
            ) if terminal else 0,
            "file_lock": lock,
            "retry_started": False,
        }
        if not (retry_owned_lock and terminal and passed is False and lock.get("detected")):
            return response

        if not lock.get("safe_auto_stop"):
            return {
                **response,
                "retry_reason": (
                    "FOREIGN_OR_UNRESOLVED_BLOCKER"
                    if lock.get("foreign_blockers") or lock.get("unresolved_blocker")
                    else "NO_OWNED_BLOCKER"
                ),
            }

        stopped = self.processes.stop_owned_blockers(lock)
        if not stopped.get("safe"):
            return {
                **response,
                "owned_blocker_stop": stopped,
                "retry_reason": "OWNED_BLOCKER_STOP_FAILED",
            }

        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        profile_id = str(metadata.get("profile_id") or "")
        retry_job_id = f"{job_id}_R1"
        retry = self._start_runtime_job(
            "build",
            retry_job_id,
            profile_id,
            external_env=None,
        )
        return {
            **response,
            "owned_blocker_stop": stopped,
            "retry_started": True,
            "retry_job_id": retry_job_id,
            "retry_job": retry,
            "retry_reason": "OWNED_BLOCKER_STOPPED",
        }

    def runtime_job_logs(
        self,
        job_id: str,
        *,
        stream: str = "stdout",
        cursor: int = 0,
        max_bytes: int = 16384,
    ) -> dict:
        return self.processes.job_logs(
            job_id,
            stream=stream,
            cursor=cursor,
            max_bytes=max_bytes,
        )

    def smart_test(
        self,
        profile_id: str = "",
        timeout: int = 600,
        *,
        external_env: dict[str, str] | None = None,
    ) -> dict:
        started_at = time.monotonic()
        profiles = self.profiles()
        selected = self.profile(profile_id, profiles=profiles) if profile_id else None
        commands = []
        if selected and selected.get("test_argv"):
            commands.append((selected["id"], list(selected["test_argv"]), selected))
        elif not profile_id:
            for item in profiles:
                if item.get("test_argv"):
                    commands.append((item["id"], list(item["test_argv"]), item))
            if not commands:
                workspace_profile = next((item for item in profiles if item.get("workspace_test_argv")), None)
                if workspace_profile:
                    commands.append(("workspace", list(workspace_profile["workspace_test_argv"]), workspace_profile))
        results = []
        for identifier, argv, command_profile in commands:
            process_env, env_meta = self.resolve_environment(
                command_profile,
                external_env=external_env,
            )
            result = self.processes.run(
                argv,
                timeout=timeout,
                env=process_env,
                redact_values=self._redaction_values(command_profile, process_env),
            )
            results.append({"profile": identifier, "environment": env_meta, **result})
            if result.get("returncode"):
                break
        if commands:
            summary = self._test_summary(results)
            return {
                "passed": all(item.get("returncode") == 0 for item in results),
                "mode": "test",
                "results": results,
                "requested": len(commands),
                "completed": len(results),
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                **summary,
                "test_summary": summary,
            }

        target = selected or self.profile("", profiles=profiles)
        if not target.get("run_argv"):
            raise RuntimeError("No tests or runnable smoke profile detected")
        process_id = f"SMOKE{int(time.time() * 1000) % 1000000}"
        smoke_env, smoke_env_meta = self.resolve_environment(
            target,
            external_env=external_env,
        )
        started = self.processes.start(
            list(target["run_argv"]),
            process_id,
            env=smoke_env,
            redact_values=self._redaction_values(target, smoke_env),
        )
        time.sleep(min(3.0, max(0.5, timeout / 10)))
        status = next((item for item in self.processes.list() if item["process_id"] == process_id), {})
        logs = self.processes.logs(process_id, limit=12000)
        self.processes.stop(process_id)
        smoke_return = status.get("returncode")
        smoke_summary = {
            "finished": True,
            "return_code": 0 if smoke_return is None else int(smoke_return),
            "tests_passed": 0,
            "tests_failed": 0,
            "tests_skipped": 0,
            "build_errors_count": 0,
            "test_failures_count": 0,
        }
        return {
            "passed": status.get("state") in {"RUNNING", "EXITED"} and smoke_return in {None, 0},
            "mode": "startup-smoke",
            "profile": target,
            "process": started,
            "environment": smoke_env_meta,
            "status": status,
            "logs": logs,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            **smoke_summary,
            "test_summary": smoke_summary,
        }

    def _http_get(self, url: str, timeout: float = 4.0, max_bytes: int = 2_000_000) -> dict:
        started = time.monotonic()
        try:
            parsed = urlsplit(url)
            # Evidence probes are HTTP requests, never local-file or other
            # scheme readers. Reject credentials in URLs before they can be
            # serialized into result metadata and logs.
            if (parsed.scheme not in {"http", "https"} or
                    not parsed.hostname or
                    parsed.username is not None or parsed.password is not None):
                raise ValueError("INVALID_HTTP_PROBE_URL")
            request = urllib.request.Request(url, headers={"User-Agent": "EasyChange/0.1"})
            # Prefix matching is unsafe: https://localhost.attacker.test
            # is not loopback and must retain certificate verification.
            loopback_tls = (parsed.scheme == "https" and
                            parsed.hostname in {"localhost", "127.0.0.1", "::1"})
            context = ssl._create_unverified_context() if loopback_tls else None
            # A permissive localhost certificate context must never follow a
            # 302 into an unrelated host. Keep same-origin Swagger redirects.
            handlers=[
                urllib.request.ProxyHandler({})
                if parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                else urllib.request.ProxyHandler(),
                urllib.request.HTTPSHandler(context=context),
                _SameOriginRuntimeRedirect(),
            ]
            opener=urllib.request.build_opener(*handlers)
            with opener.open(request, timeout=timeout) as response:
                body = response.read(max_bytes + 1)
                return {"ok": True, "url": url, "status": int(response.status),
                        "content_type": response.headers.get("Content-Type", ""),
                        "body": body[:max_bytes], "truncated": len(body) > max_bytes,
                        "duration_ms": int((time.monotonic() - started) * 1000)}
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(max_bytes + 1)
                return {"ok": False, "url": url, "status": int(exc.code),
                        "body": body[:max_bytes], "truncated": len(body) > max_bytes,
                        "content_type": exc.headers.get("Content-Type", ""),
                        "duration_ms": int((time.monotonic() - started) * 1000)}
            finally:
                # HTTPError retains its HTTP response/socket until closed.
                # Repeated 401/404 Swagger probes must not leak descriptors.
                exc.close()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return {"ok": False, "url": url, "error": type(exc).__name__,
                    "message": str(exc)[:500], "duration_ms": int((time.monotonic() - started) * 1000)}

    def wait_for_http(self, urls: list[str], timeout: float = 35.0) -> dict:
        deadline = time.monotonic() + max(1.0, timeout)
        last = {}
        while True:
            if time.monotonic() >= deadline:
                break
            for url in urls:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Do not enforce a 0.5-second *minimum* socket timeout
                # after the total startup deadline is almost exhausted.
                result = self._http_get(url, timeout=min(2.0, remaining))
                last = result
                # A 401/403/404 proves that *some* HTTP server answered,
                # not that the requested Swagger/OpenAPI endpoint is ready.
                status=result.get("status")
                if result.get("ok") is True and type(status) is int and 200 <= status < 300:
                    return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.35, remaining))
        return {"ok": False, "error": "HTTP_STARTUP_TIMEOUT", "last": last, "urls": urls}

    def swagger_evidence(
        self,
        process_id: str,
        profile_id: str = "",
        url: str = "",
        timeout: float = 45.0,
        screenshot: bool = True,
        start_if_needed: bool = True,
        keep_running: bool = False,
        *,
        external_env: dict[str, str] | None = None,
    ) -> dict:
        profile = self.profile(profile_id)
        started_process = None
        candidates = [url] if url else list(profile.get("swagger_candidates") or [])
        if not candidates:
            candidates = self._swagger_candidates(list(profile.get("urls") or []))
        if not candidates:
            raise RuntimeError("No Swagger/OpenAPI URL detected; pass url explicitly")
        if start_if_needed and profile.get("run_argv"):
            process_env, _env_meta = self.resolve_environment(
                profile,
                external_env=external_env,
            )
            started_process = self.processes.start(
                list(profile["run_argv"]),
                process_id,
                env=process_env,
                redact_values=self._redaction_values(profile, process_env),
            )

        probe = self.wait_for_http(candidates, timeout=timeout)
        if (probe.get("ok") is not True or
                not isinstance(probe.get("status"), int) or
                not 200 <= probe["status"] < 300):
            if started_process and not keep_running:
                try:
                    self.processes.stop(process_id)
                except Exception:
                    pass
            return {"ok": False, "profile": profile, "probe": probe, "process": started_process}

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        folder = self.evidence_root / f"{stamp}-swagger"
        folder.mkdir(parents=True, exist_ok=True)
        body = probe.pop("body", b"") or b""
        suffix = ".json" if "json" in str(probe.get("content_type", "")).casefold() else ".html"
        response_path = folder / f"response{suffix}"
        response_path.write_bytes(body)
        screenshot_path = None
        if screenshot:
            visual_url = str(probe.get("url") or "")
            if visual_url.endswith((".json", "/openapi.json")):
                base = visual_url.split("/swagger/")[0].split("/openapi")[0]
                swagger_index = base.rstrip("/") + "/swagger/index.html"
                check = self._http_get(swagger_index, timeout=3.0, max_bytes=4096)
                if (check.get("ok") is True and
                        type(check.get("status")) is int and
                        200 <= check["status"] < 300):
                    visual_url = swagger_index
            screenshot_path = self._browser_screenshot(visual_url, folder / "swagger.png")

        logs = self.processes.logs(process_id, limit=20000) if started_process else None
        process_stopped = False
        if started_process and not keep_running:
            try:
                self.processes.stop(process_id)
                process_stopped = True
            except Exception:
                pass
        # HTTP 200 verifies the endpoint, not the screenshot. This runtime
        # intentionally does not spin up an automated browser; a missing
        # image must never be reported as complete visual evidence.
        screenshot_captured = False
        if screenshot_path is not None and screenshot_path == folder / "swagger.png":
            try:
                # A returned pathname is not visual evidence. Require the
                # expected regular file and PNG signature; never read the
                # whole image back into memory just to validate existence.
                if screenshot_path.is_file() and not screenshot_path.is_symlink():
                    with screenshot_path.open("rb") as image_stream:
                        screenshot_captured = (
                            image_stream.read(8) == b"\x89PNG\r\n\x1a\n")
            except OSError:
                pass
        if not screenshot_captured:
            screenshot_path = None
        evidence = {
            "created_utc": stamp, "profile": profile, "probe": probe,
            "response_file": _rel(self.workspace.root_path, response_path),
            "screenshot_file": _rel(self.workspace.root_path, screenshot_path) if screenshot_path else None,
            "http_verified": True,
            "screenshot_requested": bool(screenshot),
            "screenshot_captured": screenshot_captured,
            "process": started_process, "process_stopped": process_stopped,
            "keep_running": bool(keep_running), "logs": logs,
        }
        evidence_path = folder / "evidence.json"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path = folder / "README.md"
        report_path.write_text(
            "# EasyChange Swagger Evidence\n\n"
            f"- Created UTC: {stamp}\n"
            f"- Profile: {profile.get('id')} ({profile.get('kind')})\n"
            f"- URL: {probe.get('url')}\n"
            f"- HTTP status: {probe.get('status')}\n"
            f"- Response file: {evidence['response_file']}\n"
            f"- Screenshot file: {evidence['screenshot_file'] or 'not captured'}\n"
            f"- Process kept running: {bool(keep_running)}\n",
            encoding="utf-8",
        )
        complete = not screenshot or screenshot_captured
        return {
            "ok": complete,
            **({"error": "SWAGGER_SCREENSHOT_NOT_CAPTURED"} if not complete else {}),
            "evidence_dir": _rel(self.workspace.root_path, folder),
            "report": _rel(self.workspace.root_path, report_path),
            **evidence,
        }

    def _evidence_metadata(self, folder: Path) -> dict:
        files = []
        total_bytes = 0
        try:
            candidates = sorted(path for path in folder.rglob("*") if path.is_file())
        except OSError:
            candidates = []
        for path in candidates:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            total_bytes += size
            files.append({"path": _rel(self.workspace.root_path, path), "bytes": size})
        return {"file_count": len(files), "total_bytes": total_bytes, "files": files}
    def evidence(self, title: str = "test-evidence", process_id: str = "") -> dict:
        started_at = time.monotonic()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        folder = self.evidence_root / f"{stamp}-{_slug(title)}"
        folder.mkdir(parents=True, exist_ok=True)
        logs = self.processes.logs(process_id, limit=30000) if process_id else None
        execution = self.processes.last_execution
        data = {"created_utc": stamp, "title": title, "process_id": process_id or None,
                "logs": logs, "last_execution": execution, "runtime_profiles": self.profiles()}
        json_path = folder / "evidence.json"
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        report = folder / "README.md"
        report.write_text(
            "# EasyChange Evidence\n\n"
            f"- Created UTC: {stamp}\n- Title: {title}\n"
            f"- Process: {process_id or 'n/a'}\n"
            f"- Last execution return code: {(execution or {}).get('returncode')}\n",
            encoding="utf-8",
        )
        stdout = str((execution or {}).get("stdout") or (logs or {}).get("stdout") or "")
        stderr = str((execution or {}).get("stderr") or (logs or {}).get("stderr") or "")
        html_path = folder / "evidence.html"
        html_path.write_text(
            "<!doctype html><meta charset='utf-8'><title>EasyChange Evidence</title>"
            "<style>body{font-family:Consolas,monospace;background:#101820;color:#f2f5f7;padding:28px}"
            "pre{white-space:pre-wrap;background:#17242e;padding:18px;border-radius:8px}</style>"
            f"<h1>{html.escape(title)}</h1><p>Created UTC: {stamp}</p>"
            f"<p>Return code: {html.escape(str((execution or {}).get('returncode')))}</p>"
            f"<h2>STDOUT</h2><pre>{html.escape(stdout[-30000:])}</pre>"
            f"<h2>STDERR</h2><pre>{html.escape(stderr[-30000:])}</pre>",
            encoding="utf-8",
        )
        screenshot = self._browser_screenshot(html_path.resolve().as_uri(), folder / "evidence.png")
        metadata = self._evidence_metadata(folder)
        return {
            "evidence_dir": _rel(self.workspace.root_path, folder),
            "evidence_json": _rel(self.workspace.root_path, json_path),
            "report": _rel(self.workspace.root_path, report),
            "html_report": _rel(self.workspace.root_path, html_path),
            "screenshot_file": _rel(self.workspace.root_path, screenshot) if screenshot else None,
            **metadata,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        }

    def test_with_evidence(
        self,
        profile_id: str = "",
        timeout: int = 600,
        title: str = "test-evidence",
        *,
        external_env: dict[str, str] | None = None,
    ) -> dict:
        started_at = time.monotonic()
        test = self.smart_test(
            profile_id,
            timeout,
            external_env=external_env,
        )
        evidence = self.evidence(title)
        return {
            "passed": bool(test.get("passed")),
            "test": test,
            "evidence": evidence,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        }

    @staticmethod
    def _browser_candidates() -> list[str]:
        candidates = []
        for name in ("msedge", "msedge.exe", "chrome", "chrome.exe"):
            found = shutil.which(name)
            if found and found not in candidates:
                candidates.append(found)
        if os.name == "nt":
            for env, suffix in (
                ("PROGRAMFILES(X86)", r"Microsoft\Edge\Application\msedge.exe"),
                ("PROGRAMFILES", r"Microsoft\Edge\Application\msedge.exe"),
                ("PROGRAMFILES", r"Google\Chrome\Application\chrome.exe"),
                ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"),
            ):
                root = os.environ.get(env)
                if root:
                    path = str(Path(root) / suffix)
                    if Path(path).exists() and path not in candidates:
                        candidates.append(path)
        return candidates

    def _browser_screenshot(self, url: str, target: Path) -> Path | None:
            del url, target
            # Browser evidence must come from BrowserService attached to the
            # already-running human browser. RuntimeService never launches
            # Edge/Chrome/headless or creates a profile as an evidence shortcut.
            return None
