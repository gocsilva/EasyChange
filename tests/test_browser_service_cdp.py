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
    monkeypatch.setattr(
        browser_module.subprocess,
        "Popen",
        lambda *a, **k: _FakeBrowserProcess(return_code=None),
    )
    with pytest.raises(browser_module.BrowserBootstrapError) as exc:
        browser.start("BOOT1", timeout=0.2)
    assert exc.value.code == "CDP_PORT_NOT_OPEN"
    diag = exc.value.diagnostics
    assert diag["browser_process_started"] is True
    assert diag["cdp_address"] == "127.0.0.1"
    assert diag["cdp_port_selected"] is None
    assert diag["json_version_available"] is False
    assert diag["stage"] == "bootstrap_timeout"


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

    result = browser.restore_easychange_foreground(attempts=2)

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

    result = browser.restore_easychange_foreground(attempts=2)

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
