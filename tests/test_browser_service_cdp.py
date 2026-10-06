from __future__ import annotations
import base64
import easychange.core.browser_service as browser_module

import contextlib
import http.server
import io
import os
import threading
from pathlib import Path

import pytest

from easychange.core.browser_service import BrowserService
from easychange.core.process_service import ProcessService


@contextlib.contextmanager
def _server(root: Path):
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(
        *args, directory=str(root), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.skipif(not BrowserService.candidates(), reason="Edge/Chrome not installed")
class _FakeCdp:
    instances = []

    def __init__(self, url: str, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout
        self.calls = []
        self.closed = False
        _FakeCdp.instances.append(self)

    def call(self, method: str, params=None, timeout: float = 10.0):
        self.calls.append((method, dict(params or {})))
        if method == "Runtime.evaluate":
            expression = str((params or {}).get("expression") or "")
            if expression == "location.href":
                return {"result": {"value": "https://existing.local/swagger/index.html"}}
            if expression == "document.title":
                return {"result": {"value": "Existing Swagger"}}
            return {"result": {"value": True}}
        if method == "Browser.getVersion":
            return {"product": "Microsoft Edge/140.0"}
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7}
        if method == "Browser.getWindowBounds":
            return {"bounds": {"windowState": "normal"}}
        if method == "Page.getLayoutMetrics":
            return {"cssContentSize": {"width": 1200, "height": 1800}}
        if method == "Page.captureScreenshot":
            raw = b"\xff\xd8" + b"x" * 512 + b"\xff\xd9"
            return {"data": base64.b64encode(raw).decode("ascii")}
        return {}

    def drain_events(self, limit: int = 100):
        return [
            {"method": "Network.requestWillBeSent", "params": {"requestId": "R1"}}
        ][:max(0, int(limit))]

    def close(self):
        self.closed = True


def _attach_fake(browser, monkeypatch):
    _FakeCdp.instances.clear()

    def fake_http(url, method="GET", timeout=3.0):
        del method, timeout
        if url.endswith("/json/version"):
            return {
                "Browser": "Microsoft Edge/140.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/existing",
            }
        if url.endswith("/json/list"):
            return [{
                "id": "page-1",
                "type": "page",
                "url": "https://existing.local/swagger/index.html",
                "title": "Existing Swagger",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/page-1",
            }]
        raise AssertionError(url)

    monkeypatch.setattr(browser_module, "_WebSocket", _FakeCdp)
    monkeypatch.setattr(browser, "_http_json", fake_http)
    return browser.attach("T1", port=9222)
def test_attach_reuses_existing_browser_and_never_starts_process(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    processes = ProcessService(workspace)
    browser = BrowserService(processes)

    def forbidden(*args, **kwargs):
        raise AssertionError("browser/process launch is forbidden")

    monkeypatch.setattr(processes, "start", forbidden)
    started = _attach_fake(browser, monkeypatch)

    assert started["alive"] is True
    assert started["launch_mode"] == "attach_existing"
    assert started["existing_human_session"] is True
    assert started["browser_launched"] is False
    assert started["profile_created"] is False
    assert started["install_required"] is False
    assert started["url"].endswith("/swagger/index.html")


@pytest.mark.skipif(not BrowserService.candidates(), reason="Edge/Chrome not installed")
def test_start_forbids_headless_and_direct_browser_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    browser = BrowserService(ProcessService(workspace))

    with pytest.raises(Exception, match="BROWSER_HEADLESS_FORBIDDEN"):
        browser.start("X", headless=True)
    with pytest.raises(Exception, match="ONLY_INSTALLED_EDGE_OR_CHROME_ALLOWED"):
        browser.start("X", browser_path="C:/Browser/browser.exe")


@pytest.mark.skipif(not BrowserService.candidates(), reason="Edge/Chrome not installed")
def test_dom_is_paged_and_redacted_without_browser_io(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    browser = BrowserService(ProcessService(workspace))

    monkeypatch.setattr(
        browser,
        "_evaluate",
        lambda *args, **kwargs: {
            "html": "<html><body>token=abc123456789 password=hunter2 " + ("x" * 4000) + "</body></html>",
            "url": "https://existing.local",
            "title": "Page",
            "scrollX": 0,
            "scrollY": 100,
            "viewportWidth": 1200,
            "viewportHeight": 800,
            "totalWidth": 1200,
            "totalHeight": 2200,
            "focused": "INPUT",
        },
    )
    page1 = browser.dom("T1", cursor=0, max_bytes=1024)
    assert page1["schema"] == "easychange.browser-dom/1"
    assert page1["complete"] is False
    assert page1["next_cursor"] == 1024
    assert "hunter2" not in page1["text"]
    assert "abc123456789" not in page1["text"]
    assert "[REDACTED]" in page1["text"]

    page2 = browser.dom("T1", cursor=page1["next_cursor"], max_bytes=1024)
    assert page2["cursor"] == 1024
    assert page2["sha256"] == page1["sha256"]


@pytest.mark.skipif(not BrowserService.candidates(), reason="Edge/Chrome not installed")
def test_visual_is_in_memory_and_qr_chunk_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    browser = BrowserService(ProcessService(workspace))
    _attach_fake(browser, monkeypatch)

    monkeypatch.setattr(
        browser,
        "position",
        lambda session_id="default": {
            "url": "https://existing.local",
            "viewportWidth": 1200,
            "viewportHeight": 800,
        },
    )
    visual = browser.visual("T1", quality=55, full_page=False, max_bytes=4096)
    assert visual["mime"] == "image/jpeg"
    assert visual["bytes"] > 0
    assert visual["qr_transport"] == "EC2_RESULT_STORE_CHUNKS"
    assert base64.b64decode(visual["data_b64"]).startswith(b"\xff\xd8")
    assert "path" not in visual


@pytest.mark.skipif(os.name != "nt" or not BrowserService.candidates(), reason="Windows Edge/Chrome required")
def test_generic_cdp_call_batch_and_events_without_real_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    browser = BrowserService(ProcessService(workspace))
    _attach_fake(browser, monkeypatch)

    single = browser.cdp_call(
        "T1",
        "Performance.getMetrics",
        {"timeDomain": "timeTicks"},
        channel="page",
        include_events=True,
    )
    assert single["schema"] == "easychange.browser-cdp/1"
    assert single["method"] == "Performance.getMetrics"
    assert single["channel"] == "page"
    assert single["event_count"] == 1

    batch = browser.cdp_batch(
        "T1",
        [
            {
                "method": "Runtime.evaluate",
                "params": {
                    "expression": "location.href",
                    "returnByValue": True,
                },
                "save_as": "current",
            },
            {
                "method": "Page.navigate",
                "params": {
                    "url": {"$result_ref": "current.result.value"},
                },
                "save_as": "navigation",
            },
        ],
    )
    assert batch["ok"] is True
    assert batch["count"] == 2
    page_channel = next(
        channel for channel in _FakeCdp.instances
        if "/devtools/page/" in channel.url
    )
    assert (
        "Page.navigate",
        {"url": "https://existing.local/swagger/index.html"},
    ) in page_channel.calls

    events = browser.cdp_events("T1", channel="page", limit=10, pump=True)
    assert events["schema"] == "easychange.browser-cdp-events/1"
    assert events["count"] == 1
    assert events["events"][0]["method"] == "Network.requestWillBeSent"


def test_browser_open_is_attach_first_and_does_not_spawn_when_attach_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    browser = BrowserService(ProcessService(workspace))
    _attach_fake(browser, monkeypatch)

    monkeypatch.setattr(
        browser_module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Popen must not run when existing CDP attach succeeds")
        ),
    )
    opened = browser.start("T1", url="about:blank", visible=False)
    assert opened["attach_first"] is True
    assert opened["browser_launched"] is False
    assert opened["profile_created"] is False
    assert opened["install_required"] is False
def test_detach_never_closes_human_browser_or_process(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    processes = ProcessService(workspace)
    browser = BrowserService(processes)
    _attach_fake(browser, monkeypatch)

    monkeypatch.setattr(
        processes,
        "stop",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("process stop is forbidden")
        ),
    )
    result = browser.stop("T1")
    assert result["state"] == "DETACHED"
    assert result["browser_closed"] is False
    assert result["process_stopped"] is False
    assert result["profile_deleted"] is False
    assert result["existing_human_session_preserved"] is True
    assert all(
        method != "Browser.close"
        for channel in _FakeCdp.instances
        for method, _params in channel.calls
    )


def test_candidates_never_return_chromium_and_prefer_edge(tmp_path, monkeypatch):
    program_files = tmp_path / "Program Files"
    edge = program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    chrome = program_files / "Google" / "Chrome" / "Application" / "chrome.exe"
    edge.parent.mkdir(parents=True)
    chrome.parent.mkdir(parents=True)
    edge.write_bytes(b"edge")
    chrome.write_bytes(b"chrome")
    monkeypatch.setenv("PROGRAMFILES", str(program_files))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "Program Files x86"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    monkeypatch.setattr(browser_module.shutil, "which", lambda name: None)
    values = BrowserService.candidates()
    assert Path(values[0]).name.casefold() == "msedge.exe"
    assert "chrome.exe" in [Path(value).name.casefold() for value in values]
    assert not any("chromium" in value.casefold() for value in values)


def test_candidates_use_chrome_when_edge_is_absent(tmp_path, monkeypatch):
    program_files = tmp_path / "Program Files"
    chrome = program_files / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"chrome")
    monkeypatch.setenv("PROGRAMFILES", str(program_files))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "Program Files x86"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    monkeypatch.setattr(browser_module.shutil, "which", lambda name: None)
    values = BrowserService.candidates()
    assert values and Path(values[0]).name.casefold() == "chrome.exe"


def test_cdp_websocket_rejects_non_loopback_before_network_access():
    with pytest.raises(Exception, match="CDP_NON_LOOPBACK_FORBIDDEN"):
        browser_module._WebSocket(
            "ws://192.168.50.10:9222/devtools/page/unsafe",
            timeout=0.1,
        )


def test_browser_open_without_installed_edge_or_chrome_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    browser = BrowserService(ProcessService(workspace))
    monkeypatch.setattr(
        browser,
        "attach",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            browser_module.BrowserError("CDP_EXISTING_SESSION_NOT_AVAILABLE")
        ),
    )
    monkeypatch.setattr(browser, "candidates", lambda: [])
    with pytest.raises(FileNotFoundError, match="NO_INSTALLED_EDGE_OR_CHROME_FOUND"):
        browser.start("S1", timeout=0.5)


def test_bootstrap_diagnostics_redact_secrets_and_url_query(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    browser = BrowserService(ProcessService(workspace))
    class FakeCapture:
        def tail(self):
            return "Authorization: Bearer abcdefghijklmnop token=secretvalue password=hunter2 https://host/path?token=bad"
    safe = browser._safe_capture_tail(FakeCapture())
    assert "abcdefghijklmnop" not in safe
    assert "secretvalue" not in safe
    assert "hunter2" not in safe
    assert "?token=bad" not in safe
    assert "[REDACTED]" in safe


def test_persistent_profile_is_outside_workspace_and_survives_detach(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    browser = BrowserService(ProcessService(workspace))
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"edge")
    profile = browser._persistent_profile_dir(str(edge))
    assert str(profile).startswith(str(tmp_path / "localapp" / "EasyChange" / "BrowserProfiles"))
    assert not str(profile).startswith(str(workspace))
