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
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .process_service import ProcessService
from .workspace import Workspace

_SKIP = {".git", ".easychange", "bin", "obj", "node_modules", ".venv", "venv", "dist", "build"}


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return value[:80] or "evidence"


class ProjectRuntimeService:
    """Deterministic project runtime/test/evidence helper for Machine Mode."""

    def __init__(self, workspace: Workspace, processes: ProcessService) -> None:
        self.workspace = workspace
        self.processes = processes
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
            except ValueError:
                continue
            output.append({
                "id": str(row["id"]).strip(),
                "ecosystem": str(row.get("ecosystem") or "custom"),
                "kind": str(row.get("kind") or "custom"),
                "project": str(row.get("project") or "runtime_profiles.json"),
                "runnable": bool(run_argv),
                "run_argv": run_argv,
                "test_argv": test_argv,
                "urls": [str(item) for item in (row.get("urls") or []) if str(item).startswith(("http://", "https://"))][:16],
                "swagger_candidates": [str(item) for item in (row.get("swagger_candidates") or []) if str(item).startswith(("http://", "https://"))][:32],
                "configured": True,
            })
        return output

    def configure_profile(self, profile_id: str, *, kind: str = "custom",
                          run_argv: list[str] | None = None, test_argv: list[str] | None = None,
                          urls: list[str] | None = None) -> dict:
        profile_id = str(profile_id or "").strip()
        if not profile_id:
            raise ValueError("PROFILE_ID_REQUIRED")
        run = self._argv(run_argv)
        test = self._argv(test_argv)
        safe_urls = [str(item) for item in (urls or []) if str(item).startswith(("http://", "https://"))][:16]
        existing = self._configured_profiles()
        row = {
            "id": profile_id, "ecosystem": "custom", "kind": str(kind or "custom"),
            "project": "runtime_profiles.json", "runnable": bool(run),
            "run_argv": run, "test_argv": test, "urls": safe_urls,
            "swagger_candidates": self._swagger_candidates(safe_urls),
            "configured": True,
        }
        by_id = {item["id"]: item for item in existing}
        by_id[profile_id] = row
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps({"profiles": list(by_id.values())}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"profile": row, "config": ".easychange/runtime_profiles.json"}

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
        solution = next(iter(sorted(self.workspace.root_path.glob("*.sln"))), None)
        all_restored = bool(projects) and all((path.parent / "obj" / "project.assets.json").exists() for path in projects)
        if output and solution:
            workspace_test = ["dotnet", "test", solution.name]
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

    def start(self, process_id: str, profile_id: str = "") -> dict:
        profile = self.profile(profile_id)
        argv = profile.get("run_argv")
        if not argv:
            raise RuntimeError(f"Profile is not runnable: {profile.get('id')}")
        result = self.processes.start(list(argv), process_id)
        return {**result, "profile": profile}

    def smart_test(self, profile_id: str = "", timeout: int = 600) -> dict:
        started_at = time.monotonic()
        profiles = self.profiles()
        selected = self.profile(profile_id, profiles=profiles) if profile_id else None
        commands = []
        if selected and selected.get("test_argv"):
            commands.append((selected["id"], list(selected["test_argv"])))
        elif not profile_id:
            for item in profiles:
                if item.get("test_argv"):
                    commands.append((item["id"], list(item["test_argv"])))
            if not commands:
                workspace_test = next((item.get("workspace_test_argv") for item in profiles if item.get("workspace_test_argv")), None)
                if workspace_test:
                    commands.append(("workspace", list(workspace_test)))
        results = []
        for identifier, argv in commands:
            result = self.processes.run(argv, timeout=timeout)
            results.append({"profile": identifier, **result})
            if result.get("returncode"):
                break
        if commands:
            return {
                "passed": all(item.get("returncode") == 0 for item in results),
                "mode": "test",
                "results": results,
                "requested": len(commands),
                "completed": len(results),
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            }

        target = selected or self.profile("", profiles=profiles)
        if not target.get("run_argv"):
            raise RuntimeError("No tests or runnable smoke profile detected")
        process_id = f"SMOKE{int(time.time() * 1000) % 1000000}"
        started = self.processes.start(list(target["run_argv"]), process_id)
        time.sleep(min(3.0, max(0.5, timeout / 10)))
        status = next((item for item in self.processes.list() if item["process_id"] == process_id), {})
        logs = self.processes.logs(process_id, limit=12000)
        self.processes.stop(process_id)
        return {
            "passed": status.get("state") in {"RUNNING", "EXITED"} and status.get("returncode") in {None, 0},
            "mode": "startup-smoke",
            "profile": target,
            "process": started,
            "status": status,
            "logs": logs,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        }

    def _http_get(self, url: str, timeout: float = 4.0, max_bytes: int = 2_000_000) -> dict:
        request = urllib.request.Request(url, headers={"User-Agent": "EasyChange/0.1"})
        context = ssl._create_unverified_context() if url.startswith("https://localhost") or url.startswith("https://127.0.0.1") else None
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                body = response.read(max_bytes + 1)
                return {"ok": True, "url": url, "status": int(response.status),
                        "content_type": response.headers.get("Content-Type", ""),
                        "body": body[:max_bytes], "truncated": len(body) > max_bytes,
                        "duration_ms": int((time.monotonic() - started) * 1000)}
        except urllib.error.HTTPError as exc:
            body = exc.read(max_bytes)
            return {"ok": False, "url": url, "status": int(exc.code), "body": body,
                    "content_type": exc.headers.get("Content-Type", ""),
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"ok": False, "url": url, "error": type(exc).__name__,
                    "message": str(exc)[:500], "duration_ms": int((time.monotonic() - started) * 1000)}

    def wait_for_http(self, urls: list[str], timeout: float = 35.0) -> dict:
        deadline = time.monotonic() + max(1.0, timeout)
        last = {}
        while time.monotonic() < deadline:
            for url in urls:
                result = self._http_get(url, timeout=min(2.0, max(0.5, deadline - time.monotonic())))
                last = result
                if result.get("status") and int(result["status"]) < 500:
                    return result
            time.sleep(0.35)
        return {"ok": False, "error": "HTTP_STARTUP_TIMEOUT", "last": last, "urls": urls}

    def swagger_evidence(self, process_id: str, profile_id: str = "", url: str = "",
                         timeout: float = 45.0, screenshot: bool = True, start_if_needed: bool = True,
                         keep_running: bool = False) -> dict:
        profile = self.profile(profile_id)
        started_process = None
        candidates = [url] if url else list(profile.get("swagger_candidates") or [])
        if not candidates:
            candidates = self._swagger_candidates(list(profile.get("urls") or []))
        if not candidates:
            raise RuntimeError("No Swagger/OpenAPI URL detected; pass url explicitly")
        if start_if_needed and profile.get("run_argv"):
            started_process = self.processes.start(list(profile["run_argv"]), process_id)

        probe = self.wait_for_http(candidates, timeout=timeout)
        if not probe.get("ok") and not probe.get("status"):
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
                if check.get("status") and int(check["status"]) < 500:
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
        evidence = {
            "created_utc": stamp, "profile": profile, "probe": probe,
            "response_file": _rel(self.workspace.root_path, response_path),
            "screenshot_file": _rel(self.workspace.root_path, screenshot_path) if screenshot_path else None,
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
        return {"ok": True, "evidence_dir": _rel(self.workspace.root_path, folder),
                "report": _rel(self.workspace.root_path, report_path), **evidence}

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

    def test_with_evidence(self, profile_id: str = "", timeout: int = 600,
                           title: str = "test-evidence") -> dict:
        started_at = time.monotonic()
        test = self.smart_test(profile_id, timeout)
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
        for name in ("msedge", "msedge.exe", "chrome", "chrome.exe", "chromium", "chromium.exe"):
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
        browser = next(iter(self._browser_candidates()), None)
        if not browser:
            return None
        argv = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--window-size=1440,1200", f"--screenshot={target}", url]
        try:
            completed = subprocess.run(argv, cwd=self.workspace.root_path, capture_output=True,
                                       timeout=30, check=False, shell=False)
            return target if completed.returncode == 0 and target.exists() else None
        except (OSError, subprocess.TimeoutExpired):
            return None
