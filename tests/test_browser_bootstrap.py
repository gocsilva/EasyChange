from __future__ import annotations

import io
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import easychange.core.browser_service as browser_module
from easychange.core.browser_service import (
    BrowserBootstrapError,
    BrowserService,
    BrowserSession,
)
from easychange.core.process_service import ProcessService


class FakeProcess:
    def __init__(self, pid: int = 4242, return_code=None, stdout: str = "", stderr: str = ""):
        self.pid = pid
        self._return_code = return_code
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)

    def poll(self):
        return self._return_code


class FakeCdp:
    instances = []

    def __init__(self, url: str, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout
        self.closed = False
        self.calls = []
        FakeCdp.instances.append(self)

    def call(self, method: str, params=None, timeout: float = 10.0):
        self.calls.append((method, dict(params or {})))
        if method == "Browser.getVersion":
            return {"product": "Microsoft Edge/140.0"}
        if method == "Runtime.evaluate":
            return {"result": {"type": "string", "value": "Title"}}
        if method == "Browser.getWindowForTarget":
            return {"windowId": 1}
        if method == "Browser.getWindowBounds":
            return {"bounds": {"windowState": "normal"}}
        return {}

    def drain_events(self, limit: int = 100):
        return []

    def close(self):
        self.closed = True


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return BrowserService(ProcessService(workspace))


def _no_existing_attach(monkeypatch, browser):
    def fail(*args, **kwargs):
        raise BrowserBootstrapError(
            "CDP_EXISTING_SESSION_NOT_AVAILABLE",
            {"stage": "attach_existing"},
        )
    monkeypatch.setattr(browser, "attach", fail)


def _fake_http_edge(browser, monkeypatch, port: int = 9333, *, websocket: bool = True, targets: bool = True):
    FakeCdp.instances.clear()

    def fake_http(url: str, method: str = "GET", timeout: float = 3.0):
        del method, timeout
        if url.endswith("/json/version"):
            value = {"Browser": "Microsoft Edge/140.0"}
            if websocket:
                value["webSocketDebuggerUrl"] = f"ws://127.0.0.1:{port}/devtools/browser/abc"
            return value
        if url.endswith("/json/list"):
            if not targets:
                return []
            return [{
                "id": "page-1",
                "type": "page",
                "url": "about:blank",
                "title": "Blank",
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/page-1",
            }]
        raise AssertionError(url)

    monkeypatch.setattr(browser, "_http_json", fake_http)
    monkeypatch.setattr(browser_module, "_WebSocket", FakeCdp)


def _seed_fake_session(browser, port: int = 9333, kind: str = "edge", launch_mode: str = "attach_existing"):
    product = "Microsoft Edge/140.0" if kind == "edge" else "Chrome/140.0"
    page = FakeCdp(f"ws://127.0.0.1:{port}/devtools/page/page-1")
    browser_level = FakeCdp(f"ws://127.0.0.1:{port}/devtools/browser/abc")
    browser._sessions["T1"] = BrowserSession(
        session_id="T1",
        port=port,
        target_id="page-1",
        websocket_url=page.url,
        cdp=page,
        browser_cdp=browser_level,
        attached_at=1.0,
        browser_product=product,
        launch_mode=launch_mode,
        persistent_profile=launch_mode.endswith("persistent_easychange_profile"),
    )


def test_no_installed_edge_or_chrome_found(browser, monkeypatch):
    _no_existing_attach(monkeypatch, browser)
    monkeypatch.setattr(browser, "candidates", lambda: [])
    with pytest.raises(BrowserBootstrapError) as exc:
        browser.start("T1", timeout=1)
    assert exc.value.code == "NO_INSTALLED_EDGE_OR_CHROME_FOUND"


def test_edge_is_selected_before_chrome(browser, monkeypatch):
    _no_existing_attach(monkeypatch, browser)
    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    monkeypatch.setattr(browser, "candidates", lambda: [edge, chrome])
    seen = []

    def launch(**kwargs):
        seen.append(kwargs)
        _seed_fake_session(browser, kind="edge", launch_mode="installed_browser_persistent_easychange_profile")
        return {
            **browser.state("T1"),
            "profile_created": False,
        }

    monkeypatch.setattr(browser, "_launch_browser_attempt", launch)
    result = browser.start("T1", visible=True, timeout=1)
    assert seen[0]["browser"] == edge
    assert seen[0]["browser_kind"] == "edge"
    assert result["cdp_alive"] is True


def test_chrome_is_used_when_edge_absent(browser, monkeypatch):
    _no_existing_attach(monkeypatch, browser)
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    monkeypatch.setattr(browser, "candidates", lambda: [chrome])
    seen = []

    def launch(**kwargs):
        seen.append(kwargs)
        _seed_fake_session(browser, kind="chrome", launch_mode="installed_browser_persistent_easychange_profile")
        # Make fake Browser.getVersion identify Chrome for this session.
        browser._sessions["T1"].browser_cdp.call = lambda method, params=None, timeout=10: (
            {"product": "Chrome/140.0"} if method == "Browser.getVersion" else {}
        )
        browser._sessions["T1"].cdp.call = lambda method, params=None, timeout=10: (
            {"result": {"type": "string", "value": "Title"}}
            if method == "Runtime.evaluate"
            else {}
        )
        return {**browser.state("T1"), "profile_created": False}

    monkeypatch.setattr(browser, "_launch_browser_attempt", launch)
    result = browser.start("T1", timeout=1)
    assert seen[0]["browser"] == chrome
    assert seen[0]["browser_kind"] == "chrome"
    assert seen[0]["profile_mode"] == "persistent"
    assert result["cdp_alive"] is True


def test_free_port_selection_avoids_occupied_port(browser):
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    try:
        occupied_port = occupied.getsockname()[1]
        selected = browser._find_free_loopback_port()
        assert selected != occupied_port
        assert 1 <= selected <= 65535
    finally:
        occupied.close()


def test_process_starts_but_endpoint_never_appears_fails(browser, monkeypatch):
    monkeypatch.setattr(browser_module.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(browser, "_read_devtools_active_port", lambda profile: 9333)
    monkeypatch.setattr(
        browser,
        "_attach_port",
        lambda *a, **k: (_ for _ in ()).throw(
            BrowserBootstrapError("CDP_JSON_VERSION_UNAVAILABLE", {"stage": "json_version"})
        ),
    )
    with pytest.raises(BrowserBootstrapError) as exc:
        browser._launch_browser_attempt(
            session_id="T1",
            browser=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            browser_kind="chrome",
            profile_mode="persistent",
            timeout=0.01,
        )
    assert exc.value.code == "CDP_BOOTSTRAP_TIMEOUT"
    assert exc.value.diagnostic["browser_process_started"] is True
    assert exc.value.diagnostic["json_version_available"] is False


def test_single_instance_handoff_is_detected(browser, monkeypatch):
    monkeypatch.setattr(
        browser_module.subprocess,
        "Popen",
        lambda *a, **k: FakeProcess(return_code=0, stderr="forwarded to existing browser"),
    )
    monkeypatch.setattr(browser, "_port_listening", lambda *a, **k: False)
    monkeypatch.setattr(
        browser,
        "_attach_port",
        lambda *a, **k: (_ for _ in ()).throw(
            BrowserBootstrapError("CDP_JSON_VERSION_UNAVAILABLE")
        ),
    )
    with pytest.raises(BrowserBootstrapError) as exc:
        browser._launch_browser_attempt(
            session_id="T1",
            browser=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            browser_kind="edge",
            profile_mode="default",
            timeout=1,
        )
    assert exc.value.code == "CDP_EXISTING_BROWSER_REUSED_WITHOUT_DEBUGGING"
    assert exc.value.diagnostic["browser_process_exited"] is True
    assert exc.value.diagnostic["browser_process_exit_code"] == 0


def test_json_version_unavailable_is_specific(browser, monkeypatch):
    monkeypatch.setattr(
        browser,
        "_http_json",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("no endpoint")),
    )
    with pytest.raises(BrowserBootstrapError) as exc:
        browser._attach_port("T1", 9333, timeout=1)
    assert exc.value.code == "CDP_JSON_VERSION_UNAVAILABLE"


def test_json_version_without_websocket_fails(browser, monkeypatch):
    _fake_http_edge(browser, monkeypatch, websocket=False)
    with pytest.raises(BrowserBootstrapError) as exc:
        browser._attach_port("T1", 9333, timeout=1, expected_kind="edge")
    assert exc.value.code == "CDP_BROWSER_WEBSOCKET_MISSING"


def test_json_list_without_target_fails(browser, monkeypatch):
    _fake_http_edge(browser, monkeypatch, targets=False)
    with pytest.raises(BrowserBootstrapError) as exc:
        browser._attach_port("T1", 9333, timeout=1, expected_kind="edge")
    assert exc.value.code == "CDP_PAGE_TARGET_NOT_FOUND"


def test_websocket_browser_version_runtime_and_second_operation_are_proven(browser, monkeypatch):
    _fake_http_edge(browser, monkeypatch)
    state = browser._attach_port(
        "T1",
        9333,
        timeout=1,
        expected_kind="edge",
        launch_mode="installed_browser_persistent_easychange_profile",
        profile_dir=browser._persistent_profile_dir("edge"),
        pid=4242,
        browser_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )
    diag = browser._session("T1").launch_diagnostic
    assert state["cdp_alive"] is True
    assert state["port"] == 9333
    assert state["target_id"] == "page-1"
    assert diag["json_version_available"] is True
    assert diag["json_list_available"] is True
    assert diag["target_available"] is True
    assert diag["websocket_connected"] is True
    assert diag["browser_get_version_ok"] is True
    assert diag["runtime_evaluate_ok"] is True
    # state() itself performs a second Runtime.evaluate + Browser.getVersion.
    assert sum(1 for c in FakeCdp.instances for method, _ in c.calls if method == "Browser.getVersion") >= 2


def test_attach_existing_session_is_attach_only(browser, monkeypatch):
    _fake_http_edge(browser, monkeypatch)
    monkeypatch.setattr(browser, "_candidate_ports", lambda explicit=0: [9333])
    result = browser.attach("T1", timeout=1)
    assert result["cdp_alive"] is True
    assert result["attach_only"] is True
    assert result["browser_launched"] is False
    assert result["install_required"] is False


def test_detach_only_closes_cdp_sockets(browser):
    FakeCdp.instances.clear()
    _seed_fake_session(browser)
    result = browser.stop("T1")
    assert result["state"] == "DETACHED"
    assert result["browser_closed"] is False
    assert result["process_stopped"] is False
    assert result["profile_deleted"] is False
    assert all(c.closed for c in FakeCdp.instances)


def test_no_chromium_candidate_or_product_is_accepted(browser):
    assert browser._browser_kind("msedge.exe") == "edge"
    assert browser._browser_kind("chrome.exe") == "chrome"
    with pytest.raises(BrowserBootstrapError):
        browser._browser_kind("chromium.exe")
    assert browser._product_matches_kind("Chromium/140.0") is False


def test_persistent_profile_is_not_temporary_and_is_outside_workspace(browser):
    profile = browser._persistent_profile_dir("chrome")
    assert "EasyChange" in str(profile)
    assert "BrowserProfiles" in str(profile)
    assert "tmp" not in profile.name.casefold()
    assert profile != browser.processes.cwd
    assert not str(profile).casefold().startswith(str(browser.processes.cwd).casefold())


def test_launch_args_are_loopback_only_and_never_headless(browser, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        browser_module.subprocess,
        "Popen",
        lambda argv, **kwargs: (
            captured.update({"argv": list(argv), "kwargs": kwargs}) or FakeProcess(return_code=0)
        ),
    )
    monkeypatch.setattr(
        browser,
        "_attach_port",
        lambda *a, **k: (_ for _ in ()).throw(
            BrowserBootstrapError("CDP_JSON_VERSION_UNAVAILABLE")
        ),
    )
    with pytest.raises(BrowserBootstrapError):
        browser._launch_browser_attempt(
            session_id="T1",
            browser=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            browser_kind="edge",
            profile_mode="default",
            timeout=1,
        )
    joined = " ".join(captured["argv"]).casefold()
    assert "--remote-debugging-address=127.0.0.1" in joined
    assert "0.0.0.0" not in joined
    assert "--headless" not in joined
    assert "chromium" not in joined
    assert "webdriver" not in joined
    assert "chromedriver" not in joined
    assert captured["kwargs"]["shell"] is False


def test_websocket_rejects_non_loopback_before_connect(monkeypatch):
    monkeypatch.setattr(
        browser_module.socket,
        "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must reject before connect")),
    )
    with pytest.raises(Exception, match="CDP_NON_LOOPBACK_FORBIDDEN"):
        browser_module._WebSocket("ws://192.168.1.10:9222/devtools/page/1")


def test_chrome_bootstrap_uses_persistent_user_data_dir(browser, monkeypatch):
    _no_existing_attach(monkeypatch, browser)
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    monkeypatch.setattr(browser, "candidates", lambda: [chrome])
    seen = []

    def launch(**kwargs):
        seen.append(kwargs)
        _seed_fake_session(browser, kind="chrome", launch_mode="installed_browser_persistent_easychange_profile")
        browser._sessions["T1"].browser_cdp.call = lambda method, params=None, timeout=10: (
            {"product": "Chrome/140.0"} if method == "Browser.getVersion" else {}
        )
        browser._sessions["T1"].cdp.call = lambda method, params=None, timeout=10: (
            {"result": {"type": "string", "value": "Title"}} if method == "Runtime.evaluate" else {}
        )
        return {**browser.state("T1"), "profile_created": False}

    monkeypatch.setattr(browser, "_launch_browser_attempt", launch)
    browser.start("T1", timeout=1)
    assert [item["profile_mode"] for item in seen] == ["persistent"]


def test_edge_bootstrap_falls_back_from_default_to_persistent(browser, monkeypatch):
    _no_existing_attach(monkeypatch, browser)
    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    monkeypatch.setattr(browser, "candidates", lambda: [edge])
    modes = []

    def launch(**kwargs):
        modes.append(kwargs["profile_mode"])
        if kwargs["profile_mode"] == "default":
            raise BrowserBootstrapError(
                "CDP_EXISTING_BROWSER_REUSED_WITHOUT_DEBUGGING",
                {"stage": "launch"},
            )
        _seed_fake_session(browser, kind="edge", launch_mode="installed_browser_persistent_easychange_profile")
        return {**browser.state("T1"), "profile_created": True}

    monkeypatch.setattr(browser, "_launch_browser_attempt", launch)
    result = browser.start("T1", timeout=1)
    assert modes == ["default", "persistent"]
    assert result["cdp_alive"] is True
    assert result["persistent_profile"] is True
