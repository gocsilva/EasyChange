from __future__ import annotations
import base64
import json
from collections import deque
import easychange.core.browser_service as browser_module
import easychange.core.command_service as command_module

import contextlib
import http.server
import io
import os
import threading
from pathlib import Path

import pytest

from easychange.core.browser_service import BrowserService
from easychange.core.command_service import CommandService
from easychange.core.process_service import ProcessService
from easychange.core.workspace import Workspace


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




def _bare_async_websocket():
    ws = browser_module._WebSocket.__new__(browser_module._WebSocket)
    ws._next_id = 0
    ws._send_lock = threading.RLock()
    ws._state_lock = threading.RLock()
    ws._response_ready = threading.Condition(ws._state_lock)
    ws._responses = {}
    ws._events = deque()
    ws._event_limit = 512
    ws._events_dropped = 0
    ws._reader_error = None
    ws._closed = False
    return ws


def test_cdp_websocket_continuous_reader_correlates_calls_and_buffers_events(monkeypatch):
    ws = _bare_async_websocket()
    sent = []

    def send(payload, opcode=1):
        assert opcode == 1
        request = json.loads(payload.decode("utf-8"))
        sent.append(request)
        ws._dispatch_message({
            "method": "Network.requestWillBeSent",
            "params": {"requestId": "R-ASYNC"},
        })
        ws._dispatch_message({
            "id": request["id"],
            "result": {"product": "Microsoft Edge/140.0"},
        })

    monkeypatch.setattr(ws, "_send_frame", send)

    result = ws.call("Browser.getVersion", timeout=0.2)
    events = ws.drain_events(10)

    assert result["product"].startswith("Microsoft Edge/")
    assert sent[0]["method"] == "Browser.getVersion"
    assert events == [{
        "method": "Network.requestWillBeSent",
        "params": {"requestId": "R-ASYNC"},
    }]


def test_cdp_websocket_event_buffer_is_fifo_and_bounded():
    ws = _bare_async_websocket()
    ws._event_limit = 3
    for index in range(5):
        ws._dispatch_message({
            "method": "Runtime.consoleAPICalled",
            "params": {"index": index},
        })

    status = ws.event_status()
    assert status["pending_events"] == 3
    assert status["events_dropped"] == 2
    assert [event["params"]["index"] for event in ws.drain_events(10)] == [2, 3, 4]
    assert ws.drain_events(10) == []


def test_cdp_websocket_reader_collects_event_without_pump(monkeypatch):
    ws = _bare_async_websocket()
    values = iter([
        json.dumps({
            "method": "Network.responseReceived",
            "params": {"requestId": "R-PUSH"},
        }),
    ])

    def recv_text():
        try:
            return next(values)
        except StopIteration:
            raise browser_module.CdpError("CDP_WEBSOCKET_CLOSED")

    monkeypatch.setattr(ws, "_recv_text", recv_text)
    ws._reader = threading.Thread(target=ws._reader_loop, daemon=True)
    ws._reader.start()
    ws._reader.join(timeout=0.5)

    events = ws.drain_events(10)
    assert events[0]["method"] == "Network.responseReceived"
    assert ws._reader_error == "CdpError:CDP_WEBSOCKET_CLOSED"


def test_wait_ready_rejects_chrome_error_page(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    monkeypatch.setattr(
        browser,
        "_evaluate",
        lambda *args, **kwargs: {
            "readyState": "complete",
            "url": "chrome-error://chromewebdata/",
            "title": "localhost",
        },
    )

    with pytest.raises(browser_module.BrowserError, match="BROWSER_NAVIGATION_ERROR_PAGE"):
        browser.wait_ready("ERR", timeout=0.2)

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


class _FakeBrowserProcess:
    def __init__(self, pid=4242, return_code=None, stdout="", stderr=""):
        self.pid = pid
        self._return_code = return_code
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)

    def poll(self):
        return self._return_code


def _new_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return BrowserService(ProcessService(workspace))


def test_browser_open_process_without_cdp_endpoint_fails_explicitly(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"edge")
    monkeypatch.setattr(browser, "attach", lambda *a, **k: (_ for _ in ()).throw(
        browser_module.BrowserError("CDP_EXISTING_SESSION_NOT_AVAILABLE")
    ))
    monkeypatch.setattr(browser, "candidates", lambda: [str(edge)])
    monkeypatch.setattr(browser, "_active_port", lambda profile: 0)
    captured = {}
    def fake_popen(argv, *args, **kwargs):
        captured["argv"] = list(argv)
        return _FakeBrowserProcess(return_code=None)
    monkeypatch.setattr(browser_module.subprocess, "Popen", fake_popen)
    with pytest.raises(browser_module.BrowserBootstrapError) as exc:
        browser.start("BOOT1", url="https://secret.local/path?token=hidden", timeout=0.2)
    assert exc.value.code == "CDP_PORT_NOT_OPEN"
    diag = exc.value.diagnostics
    assert diag["browser_process_started"] is True
    assert diag["cdp_address"] == "127.0.0.1"
    assert diag["cdp_port_selected"] is None
    assert diag["json_version_available"] is False
    assert diag["stage"] == "bootstrap_timeout"
    assert "--no-startup-window" in captured["argv"]
    assert "--restore-last-session" not in captured["argv"]
    assert not any("secret.local" in str(arg) for arg in captured["argv"])


def test_browser_open_detects_single_instance_handoff_without_debugging(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"edge")
    monkeypatch.setattr(browser, "attach", lambda *a, **k: (_ for _ in ()).throw(
        browser_module.BrowserError("CDP_EXISTING_SESSION_NOT_AVAILABLE")
    ))
    monkeypatch.setattr(browser, "candidates", lambda: [str(edge)])
    monkeypatch.setattr(browser, "_active_port", lambda profile: 0)
    monkeypatch.setattr(
        browser_module.subprocess,
        "Popen",
        lambda *a, **k: _FakeBrowserProcess(
            return_code=0,
            stderr="forwarded to an existing browser instance",
        ),
    )
    with pytest.raises(browser_module.BrowserBootstrapError) as exc:
        browser.start("BOOT2", timeout=2)
    assert exc.value.code == "CDP_EXISTING_BROWSER_REUSED_WITHOUT_DEBUGGING"
    assert exc.value.diagnostics["browser_process_exited"] is True
    assert exc.value.diagnostics["return_code"] == 0


def test_attach_json_version_without_browser_websocket_fails_specific(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    monkeypatch.setattr(
        browser,
        "_http_json",
        lambda url, method="GET", timeout=3.0: (
            {"Browser": "Microsoft Edge/140.0"}
            if url.endswith("/json/version")
            else []
        ),
    )
    with pytest.raises(browser_module.BrowserBootstrapError) as exc:
        browser.attach("A1", port=9333, timeout=0.5)
    assert exc.value.code == "CDP_BROWSER_WEBSOCKET_MISSING"
    assert exc.value.diagnostics["attempts"][-1]["stage"] == "json_version"


def test_attach_json_list_without_page_target_fails_specific(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)

    def fake_http(url, method="GET", timeout=3.0):
        del method, timeout
        if url.endswith("/json/version"):
            return {
                "Browser": "Microsoft Edge/140.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/1",
            }
        if url.endswith("/json/list"):
            return []
        raise AssertionError(url)

    monkeypatch.setattr(browser, "_http_json", fake_http)
    with pytest.raises(browser_module.BrowserBootstrapError) as exc:
        browser.attach("A2", port=9333, timeout=0.5)
    assert exc.value.code == "CDP_PAGE_TARGET_NOT_FOUND"
    assert exc.value.diagnostics["attempts"][-1]["stage"] == "json_list"


def test_attach_rejects_chromium_product_even_on_loopback(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    monkeypatch.setattr(
        browser,
        "_http_json",
        lambda url, method="GET", timeout=3.0: (
            {
                "Browser": "Chromium/140.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/1",
            }
            if url.endswith("/json/version")
            else []
        ),
    )
    with pytest.raises(browser_module.BrowserBootstrapError) as exc:
        browser.attach("A3", port=9333, timeout=0.5)
    assert exc.value.code == "CDP_ENDPOINT_IS_NOT_EDGE_OR_CHROME"


def test_attach_proves_browser_get_version_runtime_and_second_cdp_operation(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    _attach_fake(browser, monkeypatch)
    page = next(item for item in _FakeCdp.instances if "/devtools/page/" in item.url)
    root = next(item for item in _FakeCdp.instances if "/devtools/browser/" in item.url)
    page_methods = [method for method, _ in page.calls]
    root_methods = [method for method, _ in root.calls]
    assert "Browser.getVersion" in root_methods
    assert page_methods.count("Runtime.evaluate") >= 2
    assert {"Page.enable", "Runtime.enable", "DOM.enable"}.issubset(page_methods)
    state = browser.state("T1")
    assert state["cdp_alive"] is True
    assert state["port"] == 9222
    assert state["target_id"] == "page-1"


def test_independent_attach_rediscovers_persistent_profile_devtools_port(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"edge")
    profile = browser._persistent_profile_dir(str(edge))
    (profile / "DevToolsActivePort").write_text("9333\n/devtools/browser/abc\n", encoding="utf-8")

    def fake_http(url, method="GET", timeout=3.0):
        del method, timeout
        if url.endswith("/json/version"):
            return {
                "Browser": "Microsoft Edge/140.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/existing",
            }
        if url.endswith("/json/list"):
            return [{
                "id": "page-persistent",
                "type": "page",
                "url": "https://localhost/swagger/index.html",
                "title": "Swagger",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/page-persistent",
            }]
        raise AssertionError(url)

    monkeypatch.setattr(browser_module, "_WebSocket", _FakeCdp)
    monkeypatch.setattr(browser, "_http_json", fake_http)
    result = browser.attach("REDISCOVER", timeout=1)
    assert result["cdp_alive"] is True
    assert result["port"] == 9333
    assert result["profile_mode"] == "easychange_persistent_nondefault"
    assert result["discovered_via_devtools_active_port"] is True


def test_bootstrap_launch_never_uses_chromium_headless_or_network_binding(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"edge")
    monkeypatch.setattr(browser, "attach", lambda *a, **k: (_ for _ in ()).throw(
        browser_module.BrowserError("CDP_EXISTING_SESSION_NOT_AVAILABLE")
    ))
    monkeypatch.setattr(browser, "candidates", lambda: [str(edge)])
    monkeypatch.setattr(browser, "_active_port", lambda profile: 0)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = dict(kwargs)
        return _FakeBrowserProcess(return_code=0)

    monkeypatch.setattr(browser_module.subprocess, "Popen", fake_popen)
    with pytest.raises(browser_module.BrowserBootstrapError):
        browser.start("SAFE", timeout=2)
    command = " ".join(captured["argv"]).casefold()
    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--remote-debugging-port=0" in command
    assert "--user-data-dir=" in command
    assert "0.0.0.0" not in command
    assert "--headless" not in command
    assert "chromium" not in command
    assert "webdriver" not in command
    assert "chromedriver" not in command
    assert captured["kwargs"]["shell"] is False


class _FakeUser32Focus:
    def __init__(self, *, allow_focus: bool = True):
        self.foreground = 200
        self.allow_focus = allow_focus
        self.attach_calls = []
        self.show_calls = []
        self.set_foreground_calls = []
        self.active_calls = []
        self.focus_calls = []

    def GetForegroundWindow(self):
        return self.foreground

    def IsWindowVisible(self, hwnd):
        return int(hwnd) == 100

    def GetWindowThreadProcessId(self, hwnd, _pid):
        return {100: 10, 200: 20}.get(int(hwnd), 0)

    def AttachThreadInput(self, left, right, attach):
        self.attach_calls.append((int(left), int(right), bool(attach)))
        return True

    def IsIconic(self, hwnd):
        return False

    def ShowWindow(self, hwnd, mode):
        self.show_calls.append((int(hwnd), int(mode)))
        return True

    def BringWindowToTop(self, hwnd):
        return True

    def SetForegroundWindow(self, hwnd):
        self.set_foreground_calls.append(int(hwnd))
        if self.allow_focus:
            self.foreground = int(hwnd)
        return bool(self.allow_focus)

    def SetActiveWindow(self, hwnd):
        self.active_calls.append(int(hwnd))
        return int(hwnd)

    def SetFocus(self, hwnd):
        self.focus_calls.append(int(hwnd))
        return int(hwnd)


class _FakeKernel32Focus:
    def GetCurrentThreadId(self):
        return 30


def test_restore_easychange_foreground_uses_attach_thread_input_and_confirms(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    browser.set_focus_window_handle_provider(lambda: 100)
    user32 = _FakeUser32Focus(allow_focus=True)
    kernel32 = _FakeKernel32Focus()
    monkeypatch.setattr(browser_module, "_windows_user32", lambda: user32)
    monkeypatch.setattr(browser_module, "_windows_kernel32", lambda: kernel32)

    result = browser.restore_easychange_foreground(attempts=2, stable_checks=1, stable_interval=0)

    assert result["command_focus_confirmed"] is True
    assert result["easychange_foreground"] is True
    assert result["focus_restore_attempted"] is True
    assert result["focus_restore_attempts"] == 1
    assert result["focus_restore_strategy"] == "ATTACH_THREAD_INPUT"
    assert user32.set_foreground_calls == [100]
    assert (30, 20, True) in user32.attach_calls
    assert (30, 10, True) in user32.attach_calls
    assert (30, 10, False) in user32.attach_calls
    assert (30, 20, False) in user32.attach_calls


def test_restore_easychange_foreground_fails_closed_when_windows_refuses_focus(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    browser.set_focus_window_handle_provider(lambda: 100)
    user32 = _FakeUser32Focus(allow_focus=False)
    monkeypatch.setattr(browser_module, "_windows_user32", lambda: user32)
    monkeypatch.setattr(browser_module, "_windows_kernel32", lambda: _FakeKernel32Focus())

    result = browser.restore_easychange_foreground(attempts=2, stable_checks=1, stable_interval=0)

    assert result["command_focus_confirmed"] is False
    assert result["easychange_foreground"] is False
    assert result["focus_restore_attempted"] is True
    assert result["focus_restore_attempts"] == 2
    assert len(user32.set_foreground_calls) == 2


def test_dom_script_avoids_literal_newline_inside_javascript_string(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    captured = {}

    def fake_evaluate(session_id, expression, timeout=10.0):
        captured["session_id"] = session_id
        captured["expression"] = expression
        captured["timeout"] = timeout
        return {
            "html": "<!doctype html>\n<html><body>ok</body></html>",
            "url": "data:text/html,test",
            "title": "test",
            "scrollX": 0,
            "scrollY": 0,
            "viewportWidth": 800,
            "viewportHeight": 600,
            "totalWidth": 800,
            "totalHeight": 600,
            "focused": "BODY",
        }

    monkeypatch.setattr(browser, "_evaluate", fake_evaluate)

    result = browser.dom("dom-test", cursor=0, max_bytes=2048)

    assert result["complete"] is True
    assert "String.fromCharCode(10)" in captured["expression"]
    assert "<!doctype html>\n'" not in captured["expression"]
    assert "ok" in result["text"]


def test_restore_easychange_foreground_requires_stable_confirmation_after_focus_race(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    browser.set_focus_window_handle_provider(lambda: 100)
    user32 = _FakeUser32Focus(allow_focus=True)
    kernel32 = _FakeKernel32Focus()
    monkeypatch.setattr(browser_module, "_windows_user32", lambda: user32)
    monkeypatch.setattr(browser_module, "_windows_kernel32", lambda: kernel32)

    states = iter([
        False,  # initial state before first restore
        True,   # first confirmation after SetForegroundWindow
        False,  # browser steals focus back during settle
        True,   # second restore confirmation 1
        True,   # confirmation 2
        True,   # confirmation 3
    ])

    def fake_focus_state():
        confirmed = next(states, True)
        return {
            "focus_required": True,
            "easychange_hwnd": 100,
            "easychange_visible": True,
            "easychange_foreground": confirmed,
            "command_focus_confirmed": confirmed,
            "focus_supported": True,
            "foreground_hwnd": 100 if confirmed else 200,
            "focus_validation": "WIN32_FOREGROUND_WINDOW",
        }

    monkeypatch.setattr(browser, "easychange_focus_state", fake_focus_state)

    result = browser.restore_easychange_foreground(
        attempts=3,
        stable_checks=3,
        stable_interval=0,
    )

    assert result["command_focus_confirmed"] is True
    assert result["focus_restore_attempts"] == 2
    assert result["focus_restore_stable_checks"] == 3
    assert result["focus_restore_required_stable_checks"] == 3
    assert user32.set_foreground_calls == [100, 100]


def test_restore_easychange_foreground_respects_user_minimize(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    browser.set_focus_window_handle_provider(lambda: 100)
    user32 = _FakeUser32Focus(allow_focus=True)
    user32.IsIconic = lambda hwnd: True
    monkeypatch.setattr(browser_module, "_windows_user32", lambda: user32)
    monkeypatch.setattr(browser_module, "_windows_kernel32", lambda: _FakeKernel32Focus())

    result = browser.restore_easychange_foreground(
        attempts=2,
        stable_checks=1,
        stable_interval=0,
        respect_user_state=True,
    )

    assert result["command_focus_confirmed"] is False
    assert result["focus_restore_attempted"] is False
    assert result["focus_restore_skipped"] == "USER_MINIMIZED"
    assert result["user_focus_override"] is True
    assert user32.set_foreground_calls == []
    assert user32.show_calls == []


def test_browser_focus_recovery_preserves_user_switch_to_other_application(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    browser.set_focus_window_handle_provider(lambda: 100)
    user32 = _FakeUser32Focus(allow_focus=True)
    monkeypatch.setattr(browser_module, "_windows_user32", lambda: user32)
    monkeypatch.setattr(browser_module, "_windows_kernel32", lambda: _FakeKernel32Focus())
    monkeypatch.setattr(browser, "_foreground_process_name", lambda: "devenv.exe")

    result = browser.restore_easychange_foreground(
        attempts=2,
        stable_checks=1,
        stable_interval=0,
        respect_user_state=True,
        require_browser_foreground=True,
    )

    assert result["focus_restore_attempted"] is False
    assert result["focus_restore_skipped"] == "USER_SWITCHED_APPLICATION"
    assert result["foreground_process"] == "devenv.exe"
    assert result["user_focus_override"] is True
    assert user32.set_foreground_calls == []


def test_browser_focus_recovery_may_reclaim_focus_from_edge(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    browser.set_focus_window_handle_provider(lambda: 100)
    user32 = _FakeUser32Focus(allow_focus=True)
    monkeypatch.setattr(browser_module, "_windows_user32", lambda: user32)
    monkeypatch.setattr(browser_module, "_windows_kernel32", lambda: _FakeKernel32Focus())
    monkeypatch.setattr(browser, "_foreground_process_name", lambda: "msedge.exe")

    result = browser.restore_easychange_foreground(
        attempts=2,
        stable_checks=1,
        stable_interval=0,
        respect_user_state=True,
        require_browser_foreground=True,
    )

    assert result["command_focus_confirmed"] is True
    assert result["focus_restore_attempted"] is True
    assert user32.set_foreground_calls == [100]


@pytest.mark.skipif(not BrowserService.candidates(), reason="Edge/Chrome not installed")
def test_visual_keeps_screenshot_when_position_metadata_times_out(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    browser = BrowserService(ProcessService(workspace))
    _attach_fake(browser, monkeypatch)

    def position_timeout(session_id="default"):
        raise TimeoutError("window metadata timed out")

    monkeypatch.setattr(browser, "position", position_timeout)
    visual = browser.visual("T1", quality=20, full_page=False, max_bytes=4096)

    assert visual["mime"] == "image/jpeg"
    assert visual["bytes"] > 0
    assert visual["position"] is None
    assert visual["position_error"].startswith("TimeoutError:")
    assert base64.b64decode(visual["data_b64"]).startswith(b"\xff\xd8")


@pytest.mark.skipif(not BrowserService.candidates(), reason="Edge/Chrome not installed")
def test_visual_reconnects_once_after_transient_screenshot_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    browser = BrowserService(ProcessService(workspace))
    _attach_fake(browser, monkeypatch)

    session = browser._session("T1")
    original_call = session.cdp.call
    state = {"timeouts": 0, "reconnects": 0}

    def flaky_call(method, params=None, timeout=10.0):
        if method == "Page.captureScreenshot" and state["timeouts"] == 0:
            state["timeouts"] += 1
            raise TimeoutError("transient screenshot timeout")
        return original_call(method, params, timeout)

    session.cdp.call = flaky_call
    replacement = _FakeCdp(session.websocket_url)

    def reconnect(session_id):
        assert session_id == "T1"
        state["reconnects"] += 1
        return replacement

    monkeypatch.setattr(browser, "_reconnect_current_target_socket", reconnect)
    monkeypatch.setattr(
        browser,
        "position",
        lambda session_id="default": {"url": "https://existing.local"},
    )

    visual = browser.visual("T1", quality=20, full_page=False, max_bytes=32768)

    assert visual["bytes"] > 0
    assert state == {"timeouts": 1, "reconnects": 1}
    assert visual["attempts"][0]["error"] == "TimeoutError"
    assert visual["attempts"][0]["recovery"] == "RECONNECT_CURRENT_TARGET_ONCE"
    assert any(call[0] == "Page.captureScreenshot" for call in replacement.calls)


def test_browser_open_gets_one_bounded_second_focus_pass_after_launch_race(tmp_path, monkeypatch):
    service = CommandService(Workspace.open(tmp_path))
    try:
        monkeypatch.setattr(
            service.browser,
            "start",
            lambda *args, **kwargs: {
                "alive": True,
                "browser_launched": True,
                "url": "data:text/html,focus",
            },
        )
        focus_calls = []
        responses = [
            {
                "focus_required": True,
                "command_focus_confirmed": False,
                "easychange_foreground": False,
            },
            {
                "focus_required": True,
                "command_focus_confirmed": True,
                "easychange_foreground": True,
            },
        ]

        def restore(**kwargs):
            focus_calls.append(dict(kwargs))
            return dict(responses[min(len(focus_calls) - 1, 1)])

        monkeypatch.setattr(service.browser, "restore_easychange_foreground", restore)
        monkeypatch.setattr(command_module.time, "sleep", lambda seconds: None)

        result = service._execute_structured({
            "op": "operation",
            "type": "browser_open",
            "session_id": "focus-race",
            "url": "data:text/html,focus",
            "visible": False,
            "timeout": 10,
        })

        assert result.ok is True
        assert len(focus_calls) == 2
        assert result.data["command_focus_confirmed"] is True
        assert result.data["easychange_focus"]["focus_restore_second_pass"] is True
    finally:
        service.close()


def test_browser_operation_user_focus_override_is_not_reported_as_failure(tmp_path, monkeypatch):
    service = CommandService(Workspace.open(tmp_path))
    try:
        monkeypatch.setattr(
            service.browser,
            "start",
            lambda *args, **kwargs: {
                "alive": True,
                "browser_launched": True,
                "url": "data:text/html,focus",
            },
        )
        focus_calls = []

        def restore(**kwargs):
            focus_calls.append(dict(kwargs))
            return {
                "focus_required": True,
                "command_focus_confirmed": False,
                "easychange_foreground": False,
                "focus_restore_attempted": False,
                "focus_restore_skipped": "USER_SWITCHED_APPLICATION",
                "user_focus_override": True,
            }

        monkeypatch.setattr(service.browser, "restore_easychange_foreground", restore)
        monkeypatch.setattr(command_module.time, "sleep", lambda seconds: None)

        result = service._execute_structured({
            "op": "operation",
            "type": "browser_open",
            "session_id": "focus-user-override",
            "url": "data:text/html,focus",
            "visible": False,
            "timeout": 10,
        })

        assert result.ok is True
        assert len(focus_calls) == 1
        assert result.data["command_focus_confirmed"] is False
        assert result.data["easychange_focus"]["user_focus_override"] is True
    finally:
        service.close()


def test_background_target_bootstrap_uses_browser_websocket_and_background_true(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    state = {"created": False, "calls": []}
    target_id = "TARGET-BG-1"
    page_ws = "ws://127.0.0.1:9333/devtools/page/TARGET-BG-1"
    browser_ws = "ws://127.0.0.1:9333/devtools/browser/BROWSER-1"

    def fake_http(url, method="GET", timeout=3.0):
        if url.endswith("/json/version"):
            return {
                "Browser": "Edg/154.0",
                "webSocketDebuggerUrl": browser_ws,
            }
        if url.endswith("/json/list"):
            if not state["created"]:
                return []
            return [{
                "id": target_id,
                "type": "page",
                "url": "data:text/html,<title>Background</title>",
                "title": "Background",
                "webSocketDebuggerUrl": page_ws,
            }]
        raise AssertionError(url)

    class FakeBrowserWs:
        def __init__(self, url, timeout=5.0):
            assert url == browser_ws
            self.url = url
        def call(self, method, params=None, timeout=10.0):
            state["calls"].append((method, dict(params or {})))
            assert method == "Target.createTarget"
            assert params["background"] is True
            assert params["url"] == "data:text/html,<title>Background</title>"
            state["created"] = True
            return {"targetId": target_id}
        def close(self):
            state["closed"] = True

    monkeypatch.setattr(browser, "_http_json", fake_http)
    monkeypatch.setattr(browser_module, "_WebSocket", FakeBrowserWs)

    actual_id, created = browser._ensure_background_page_target(
        9333,
        "data:text/html,<title>Background</title>",
        timeout=0.5,
    )

    assert actual_id == target_id
    assert created is True
    assert state["calls"] == [(
        "Target.createTarget",
        {
            "url": "data:text/html,<title>Background</title>",
            "background": True,
        },
    )]
    assert state["closed"] is True


def test_browser_open_attach_failure_enters_durable_controlled_profile_fallback(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"edge")
    primary = tmp_path / "profiles" / "edge"
    primary.mkdir(parents=True)

    monkeypatch.setattr(
        browser,
        "attach",
        lambda *a, **k: (_ for _ in ()).throw(
            browser_module.BrowserBootstrapError(
                "CDP_EXISTING_SESSION_NOT_AVAILABLE",
                {"localhost_only": True},
            )
        ),
    )
    monkeypatch.setattr(browser, "candidates", lambda: [str(edge)])
    monkeypatch.setattr(
        browser,
        "_persistent_profile_candidates",
        lambda browser_path: [("primary", primary)],
    )
    monkeypatch.setattr(browser, "_browser_process_present", lambda path: True)

    calls = []
    def launch(session_id, **kwargs):
        calls.append((session_id, dict(kwargs)))
        return {
            "session_id": session_id,
            "cdp_alive": True,
            "alive": True,
            "profile_mode": "easychange_persistent_nondefault",
            "profile_slot": kwargs["profile_slot"],
            "bootstrap": {
                "fallback_attempted": kwargs["fallback_attempted"],
                "fallback_strategy": kwargs["fallback_strategy"],
                "existing_browser_detected": kwargs["existing_browser_detected"],
            },
        }

    monkeypatch.setattr(browser, "_launch_controlled_profile", launch)

    result = browser.start(
        "FALLBACK1",
        url="https://localhost:65230/swagger/index.html",
        visible=False,
        timeout=1,
    )

    assert result["cdp_alive"] is True
    assert result["fallback_attempted"] is True
    assert result["fallback_success"] is True
    assert result["fallback_strategy"] == "durable_controlled_profile"
    assert len(calls) == 1
    assert calls[0][1]["profile_slot"] == "primary"
    assert calls[0][1]["existing_browser_detected"] is True
    assert calls[0][1]["fallback_attempted"] is True


def test_existing_browser_reuse_rotates_to_next_durable_profile(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"edge")
    primary = tmp_path / "profiles" / "edge"
    fallback = tmp_path / "profiles" / "edge-fallback-1"
    primary.mkdir(parents=True)
    fallback.mkdir(parents=True)

    monkeypatch.setattr(
        browser,
        "attach",
        lambda *a, **k: (_ for _ in ()).throw(
            browser_module.BrowserBootstrapError(
                "CDP_EXISTING_SESSION_NOT_AVAILABLE",
                {"localhost_only": True},
            )
        ),
    )
    monkeypatch.setattr(browser, "candidates", lambda: [str(edge)])
    monkeypatch.setattr(
        browser,
        "_persistent_profile_candidates",
        lambda browser_path: [
            ("primary", primary),
            ("fallback-1", fallback),
        ],
    )
    monkeypatch.setattr(browser, "_browser_process_present", lambda path: True)

    calls = []
    def launch(session_id, **kwargs):
        calls.append(kwargs["profile_slot"])
        if kwargs["profile_slot"] == "primary":
            raise browser_module.BrowserBootstrapError(
                "CDP_EXISTING_BROWSER_REUSED_WITHOUT_DEBUGGING",
                {
                    "stage": "existing_browser_reused_without_debugging",
                    "existing_browser_reused": True,
                    "cdp_ready": False,
                },
            )
        return {
            "session_id": session_id,
            "cdp_alive": True,
            "alive": True,
            "profile_mode": "easychange_persistent_nondefault",
            "profile_slot": kwargs["profile_slot"],
            "bootstrap": {},
        }

    monkeypatch.setattr(browser, "_launch_controlled_profile", launch)

    result = browser.start(
        "FALLBACK2",
        url="https://localhost:65230/swagger/index.html",
        visible=False,
        timeout=1,
    )

    assert calls == ["primary", "fallback-1"]
    assert result["cdp_alive"] is True
    assert result["profile_slot"] == "fallback-1"
    assert result["fallback_attempted"] is True
    assert result["fallback_success"] is True
    assert result["fallback_strategy"] == "durable_profile_rotation"


def test_edge_fallback_exhausted_uses_installed_chrome(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    edge = tmp_path / "msedge.exe"
    chrome = tmp_path / "chrome.exe"
    edge.write_bytes(b"edge")
    chrome.write_bytes(b"chrome")
    edge_profile = tmp_path / "profiles" / "edge"
    chrome_profile = tmp_path / "profiles" / "chrome"
    edge_profile.mkdir(parents=True)
    chrome_profile.mkdir(parents=True)

    monkeypatch.setattr(
        browser,
        "attach",
        lambda *a, **k: (_ for _ in ()).throw(
            browser_module.BrowserBootstrapError(
                "CDP_EXISTING_SESSION_NOT_AVAILABLE",
                {"localhost_only": True},
            )
        ),
    )
    monkeypatch.setattr(browser, "candidates", lambda: [str(edge), str(chrome)])
    monkeypatch.setattr(
        browser,
        "_persistent_profile_candidates",
        lambda browser_path: [
            (
                "primary",
                edge_profile if Path(browser_path).name == "msedge.exe" else chrome_profile,
            )
        ],
    )
    monkeypatch.setattr(browser, "_browser_process_present", lambda path: True)

    calls = []
    def launch(session_id, **kwargs):
        family = browser._browser_family(kwargs["browser"])
        calls.append(family)
        if family == "edge":
            raise browser_module.BrowserBootstrapError(
                "CDP_BOOTSTRAP_TIMEOUT",
                {"stage": "bootstrap_timeout", "cdp_ready": False},
            )
        return {
            "session_id": session_id,
            "cdp_alive": True,
            "alive": True,
            "browser": "Chrome/154.0",
            "profile_mode": "easychange_persistent_nondefault",
            "profile_slot": kwargs["profile_slot"],
            "bootstrap": {},
        }

    monkeypatch.setattr(browser, "_launch_controlled_profile", launch)

    result = browser.start("FALLBACK3", visible=False, timeout=1)

    assert calls == ["edge", "chrome"]
    assert result["cdp_alive"] is True
    assert result["fallback_attempted"] is True
    assert result["fallback_success"] is True
    assert result["fallback_strategy"] == "alternate_installed_browser"


def test_compact_success_bootstrap_diagnostics_exclude_large_tails_and_argv():
    compact = BrowserService._compact_bootstrap_diagnostics({
        "browser": "edge",
        "bootstrap_strategy": "installed_browser_durable_profile_cdp",
        "session_id": "S1",
        "profile_kind": "easychange_durable_primary",
        "profile_path_redacted": "EasyChange/BrowserProfiles/edge",
        "port": 50001,
        "target_id": "T1",
        "fallback_attempted": True,
        "fallback_strategy": "durable_controlled_profile",
        "fallback_success": True,
        "argv": ["msedge.exe", "--secret-like-large-argument"],
        "stdout_tail": "x" * 4000,
        "stderr_tail": "y" * 4000,
    })

    assert compact["browser"] == "edge"
    assert compact["port"] == 50001
    assert compact["target_id"] == "T1"
    assert compact["fallback_success"] is True
    assert "argv" not in compact
    assert "stdout_tail" not in compact
    assert "stderr_tail" not in compact



def test_swagger_execute_correlates_only_new_cdp_request(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    _attach_fake(browser, monkeypatch)

    class SwaggerCdp:
        def __init__(self):
            self.calls = []
            self.drains = 0

        def call(self, method, params=None, timeout=10.0):
            params = dict(params or {})
            self.calls.append((method, params))
            if method == "Network.enable":
                return {}
            if method == "Runtime.evaluate":
                return {"result": {"value": {"ok": True}}}
            if method == "Network.getResponseBody":
                assert params["requestId"] == "REQ-NEW"
                return {
                    "body": '{"source":"fresh","numCtrlIF":"IF-NEW"}',
                    "base64Encoded": False,
                }
            return {}

        def drain_events(self, limit=100):
            del limit
            self.drains += 1
            if self.drains == 1:
                return [
                    {
                        "method": "Network.requestWillBeSent",
                        "params": {
                            "requestId": "REQ-OLD",
                            "request": {
                                "method": "GET",
                                "url": "https://localhost:65230/cambio/easypay/acam/v1/acam225/IF-OLD",
                            },
                        },
                    },
                    {
                        "method": "Network.responseReceived",
                        "params": {
                            "requestId": "REQ-OLD",
                            "response": {"status": 200, "headers": {}},
                        },
                    },
                    {
                        "method": "Network.loadingFinished",
                        "params": {"requestId": "REQ-OLD"},
                    },
                ]
            if self.drains == 2:
                return [
                    {
                        "method": "Network.requestWillBeSent",
                        "params": {
                            "requestId": "REQ-NEW",
                            "request": {
                                "method": "GET",
                                "url": "https://localhost:65230/cambio/easypay/acam/v1/acam225/IF-NEW",
                            },
                        },
                    },
                    {
                        "method": "Network.responseReceived",
                        "params": {
                            "requestId": "REQ-NEW",
                            "response": {
                                "status": 404,
                                "headers": {"content-type": "application/json"},
                            },
                        },
                    },
                    {
                        "method": "Network.loadingFinished",
                        "params": {"requestId": "REQ-NEW"},
                    },
                ]
            return []

        def close(self):
            return None

    fake = SwaggerCdp()
    browser._session("T1").cdp = fake

    result = browser.swagger_execute(
        "T1",
        method="GET",
        path="/cambio/easypay/acam/v1/acam225/{identificador}",
        parameters={
            "identificador": "IF-NEW",
            "x-bs2-correlation-id": "CORR-NEW",
        },
        timeout=1.0,
    )

    assert result["response_correlation"] == "CDP_NETWORK_REQUEST_ID"
    assert result["response"]["status"] == "404"
    assert '"source":"fresh"' in result["response"]["body"]
    assert "IF-OLD" not in result["response"]["body"]
    body_calls = [call for call in fake.calls if call[0] == "Network.getResponseBody"]
    assert body_calls == [
        ("Network.getResponseBody", {"requestId": "REQ-NEW"})
    ]



def test_browser_open_existing_human_uses_new_tab_instead_of_navigating_human_target(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    calls = {"navigate": [], "new_tab": []}

    monkeypatch.setattr(
        browser,
        "attach",
        lambda *args, **kwargs: {
            "alive": True,
            "profile_mode": "existing_human",
            "url": "https://human.local/work",
            "target_id": "human-target",
        },
    )
    monkeypatch.setattr(
        browser,
        "new_tab",
        lambda session_id, url="about:blank", activate=True: (
            calls["new_tab"].append((session_id, url, activate))
            or {"target_id": "easychange-target", "activated": True}
        ),
    )
    monkeypatch.setattr(
        browser,
        "navigate",
        lambda *args, **kwargs: calls["navigate"].append((args, kwargs)),
    )
    monkeypatch.setattr(
        browser,
        "state",
        lambda session_id="default": {
            "alive": True,
            "profile_mode": "existing_human",
            "url": "https://swagger.local/index.html",
            "target_id": "easychange-target",
        },
    )

    result = browser.start(
        "SAFE-HUMAN",
        url="https://swagger.local/index.html",
        visible=False,
    )

    assert calls["navigate"] == []
    assert calls["new_tab"] == [
        ("SAFE-HUMAN", "https://swagger.local/index.html", True)
    ]
    assert result["target_strategy"] == "new_tab_preserve_human_target"
    assert result["easychange_created_target_id"] == "easychange-target"


def test_reused_browser_attach_missing_url_does_not_destroy_healthy_session(tmp_path, monkeypatch):
    browser = _new_browser(tmp_path, monkeypatch)
    _attach_fake(browser, monkeypatch)
    session = browser._session("T1")

    with pytest.raises(LookupError, match="BROWSER_TAB_NOT_FOUND_FOR_URL"):
        browser.attach(
            "T1",
            url_contains="definitely-not-present.example",
            timeout=0.5,
        )

    assert browser._session("T1") is session
    assert session.cdp.closed is False
    assert session.browser_cdp.closed is False
