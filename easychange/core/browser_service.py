from __future__ import annotations
import subprocess
import re

import base64
from collections import deque
import hashlib
import json
import os
import shutil
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .process_service import ProcessService


def _windows_user32():
    if os.name != "nt":
        return None
    try:
        import ctypes
        return ctypes.windll.user32
    except Exception:
        return None


def _windows_kernel32():
    if os.name != "nt":
        return None
    try:
        import ctypes
        return ctypes.windll.kernel32
    except Exception:
        return None


class BrowserError(RuntimeError):
    pass


class BrowserBootstrapError(BrowserError):
    def __init__(self, code: str, diagnostics: dict | None = None):
        self.code = str(code or "CDP_BOOTSTRAP_FAILED")
        self.diagnostics = dict(diagnostics or {})
        super().__init__(self.code)


class CdpError(BrowserError):
    pass


class _BoundedProcessCapture:
    """Drain browser stdout/stderr while retaining only a bounded safe tail."""

    def __init__(self, stream, *, max_chars: int = 16384):
        self.stream = stream
        self.max_chars = max(1024, int(max_chars))
        self._parts = deque()
        self._chars = 0
        self._lock = threading.RLock()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self.stream.readline()
                if not chunk:
                    break
                text = str(chunk)
                with self._lock:
                    self._parts.append(text)
                    self._chars += len(text)
                    while self._chars > self.max_chars and self._parts:
                        removed = self._parts.popleft()
                        self._chars -= len(removed)
        except Exception:
            return

    def tail(self) -> str:
        with self._lock:
            text = "".join(self._parts)
        return text[-self.max_chars:]


class _WebSocket:
    """Small CDP WebSocket client with one continuous reader.

    Only the reader thread consumes socket frames. Calls publish requests and
    wait on correlated response ids, while unsolicited CDP events are buffered
    continuously. This avoids the historical pump-only behavior where Network/
    console events were visible only while another command happened to be
    waiting for a response.
    """

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError("CDP_WEBSOCKET_URL_REQUIRED")
        self.host = parsed.hostname or "127.0.0.1"
        normalized_host = self.host.casefold()
        if normalized_host not in {"localhost", "::1"} and not normalized_host.startswith("127."):
            raise CdpError("CDP_NON_LOOPBACK_FORBIDDEN")
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        if parsed.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        self.sock = sock
        self.sock.settimeout(timeout)
        self._next_id = 0
        self._send_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._response_ready = threading.Condition(self._state_lock)
        self._responses: dict[int, dict] = {}
        self._events = deque()
        self._event_limit = 512
        self._events_dropped = 0
        self._reader_error: str | None = None
        self._closed = False
        self._handshake()
        # The continuous reader owns recv() after the HTTP upgrade. A short
        # socket timeout lets close() stop the daemon promptly without turning
        # idle event streams into failures.
        self.sock.settimeout(0.5)
        self._reader = threading.Thread(
            target=self._reader_loop,
            name="easychange-cdp-reader",
            daemon=True,
        )
        self._reader.start()

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response and len(response) < 65536:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
        text = bytes(response).split(b"\r\n\r\n", 1)[0].decode("latin-1", errors="replace")
        lines = text.splitlines()
        if not lines or " 101 " not in lines[0]:
            raise CdpError(f"CDP_WEBSOCKET_HANDSHAKE_FAILED:{text[:300]}")
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        accept = next(
            (
                line.split(":", 1)[1].strip()
                for line in lines[1:]
                if line.casefold().startswith("sec-websocket-accept:")
            ),
            "",
        )
        if accept != expected:
            raise CdpError("CDP_WEBSOCKET_ACCEPT_MISMATCH")

    def _recv_exact(self, count: int) -> bytes:
        output = bytearray()
        while len(output) < count:
            chunk = self.sock.recv(count - len(output))
            if not chunk:
                raise CdpError("CDP_WEBSOCKET_CLOSED")
            output.extend(chunk)
        return bytes(output)

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(bytes(header) + masked)

    def _recv_text(self) -> str:
        fragments = bytearray()
        started = False
        while True:
            first, second = self._recv_exact(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            length = second & 0x7F
            masked = bool(second & 0x80)
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 8:
                raise CdpError("CDP_WEBSOCKET_CLOSED")
            if opcode == 9:
                self._send_frame(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode == 1:
                fragments.extend(payload)
                started = True
            elif opcode == 0 and started:
                fragments.extend(payload)
            else:
                continue
            if fin:
                return fragments.decode("utf-8", errors="replace")

    def _dispatch_message(self, message: dict) -> None:
        if not isinstance(message, dict):
            return
        with self._response_ready:
            raw_id = message.get("id")
            if isinstance(raw_id, int):
                self._responses[raw_id] = message
            elif isinstance(message.get("method"), str):
                self._events.append(message)
                while len(self._events) > self._event_limit:
                    self._events.popleft()
                    self._events_dropped += 1
            self._response_ready.notify_all()

    def _reader_loop(self) -> None:
        while True:
            with self._state_lock:
                if self._closed:
                    return
            try:
                raw = self._recv_text()
            except socket.timeout:
                continue
            except Exception as exc:
                with self._response_ready:
                    if not self._closed:
                        self._reader_error = f"{type(exc).__name__}:{str(exc)[:500]}"
                    self._response_ready.notify_all()
                return
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                # A malformed CDP message is transport damage, not a valid
                # event. Keep the socket alive and surface a bounded counter by
                # recording a synthetic diagnostic event.
                message = {
                    "method": "EasyChange.cdpMalformedMessage",
                    "params": {"chars": len(raw)},
                }
            self._dispatch_message(message)

    def call(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        with self._state_lock:
            if self._closed:
                raise CdpError("CDP_WEBSOCKET_CLOSED")
            if self._reader_error:
                raise CdpError("CDP_WEBSOCKET_READER_FAILED:" + self._reader_error)
            self._next_id += 1
            call_id = self._next_id
        payload = json.dumps(
            {"id": call_id, "method": method, "params": dict(params or {})},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_frame(payload)

        deadline = time.monotonic() + max(0.1, float(timeout))
        with self._response_ready:
            while True:
                message = self._responses.pop(call_id, None)
                if isinstance(message, dict):
                    error = message.get("error")
                    if isinstance(error, dict):
                        raise CdpError(
                            f"CDP_ERROR:{method}:{error.get('code')}:{error.get('message')}"
                        )
                    result = message.get("result")
                    return result if isinstance(result, dict) else {}
                if self._reader_error:
                    raise CdpError("CDP_WEBSOCKET_READER_FAILED:" + self._reader_error)
                if self._closed:
                    raise CdpError("CDP_WEBSOCKET_CLOSED")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"CDP_TIMEOUT:{method}")
                self._response_ready.wait(timeout=min(0.25, remaining))

    def drain_events(self, limit: int = 100) -> list[dict]:
        with self._state_lock:
            count = max(0, min(self._event_limit, int(limit or 0)))
            values = []
            while self._events and len(values) < count:
                values.append(self._events.popleft())
            return values

    def event_status(self) -> dict:
        with self._state_lock:
            return {
                "reader_alive": bool(self._reader.is_alive()) if hasattr(self, "_reader") else False,
                "pending_events": len(self._events),
                "events_dropped": int(self._events_dropped),
                "reader_error": self._reader_error,
            }

    def close(self) -> None:
        with self._response_ready:
            if self._closed:
                return
            self._closed = True
            self._response_ready.notify_all()
        try:
            self._send_frame(b"", opcode=8)
        except Exception:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        reader = getattr(self, "_reader", None)
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=0.75)


@dataclass
class BrowserSession:
    session_id: str
    port: int
    target_id: str
    websocket_url: str
    cdp: _WebSocket
    browser_cdp: _WebSocket
    attached_at: float
    refs: dict[str, str] = field(default_factory=dict)
    browser_product: str = ""
    managed_process: bool = False
    process_id: str | None = None
    pid: int | None = None
    browser_path: str = ""
    profile_dir: Path | None = None
    profile_mode: str = "existing_human"
    profile_slot: str | None = None
    headless: bool = False
    launch_pid: int | None = None
    launch_diagnostics: dict[str, Any] = field(default_factory=dict)
    stdout_capture: Any = None
    stderr_capture: Any = None
    process: Any = None


class BrowserService:
    """Local-only Edge/Chrome CDP control for EasyChange on REMOTE_PC."""

    def __init__(self, processes: ProcessService) -> None:
        self.processes = processes
        self.root = processes.workspace_state_root / "browser"
        self.evidence_root = self.root / "evidence"
        self.root.mkdir(parents=True, exist_ok=True)
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = threading.RLock()
        self._focus_window_handle_provider = None

    def set_focus_window_handle_provider(self, provider) -> None:
        self._focus_window_handle_provider = provider

    def _easychange_hwnd(self) -> int:
        provider = self._focus_window_handle_provider
        if provider is None:
            return 0
        try:
            value = int(provider() or 0)
        except Exception:
            return 0
        return value if value > 0 else 0

    def easychange_focus_state(self) -> dict:
        hwnd = self._easychange_hwnd()
        base = {
            "focus_required": bool(hwnd),
            "easychange_hwnd": hwnd or None,
            "easychange_visible": None,
            "easychange_foreground": None,
            "command_focus_confirmed": False,
        }
        user32 = _windows_user32()
        if user32 is None:
            return {
                **base,
                "focus_supported": False,
                "command_focus_confirmed": True,
                "focus_validation": "NON_WINDOWS_TEST_RUNTIME",
            }
        if not hwnd:
            return {
                **base,
                "focus_supported": True,
                "focus_validation": "NO_EASYCHANGE_HWND_PROVIDER",
            }
        try:
            foreground = int(user32.GetForegroundWindow() or 0)
            visible = bool(user32.IsWindowVisible(hwnd))
            confirmed = bool(visible and foreground == hwnd)
            return {
                **base,
                "focus_supported": True,
                "foreground_hwnd": foreground or None,
                "easychange_visible": visible,
                "easychange_foreground": confirmed,
                "command_focus_confirmed": confirmed,
                "focus_validation": "WIN32_FOREGROUND_WINDOW",
            }
        except Exception as exc:
            return {
                **base,
                "focus_supported": True,
                "focus_validation": "WIN32_FOREGROUND_QUERY_FAILED",
                "focus_error": f"{type(exc).__name__}:{str(exc)[:240]}",
            }

    def _foreground_process_name(self) -> str:
        """Return the executable name that owns the current foreground window."""
        user32 = _windows_user32()
        kernel32 = _windows_kernel32()
        if user32 is None or kernel32 is None:
            return ""
        try:
            import ctypes
            foreground = int(user32.GetForegroundWindow() or 0)
            if not foreground:
                return ""
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
            if not pid.value:
                return ""
            handle = kernel32.OpenProcess(0x1000, False, int(pid.value))
            if not handle:
                return ""
            try:
                size = ctypes.c_ulong(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if not bool(
                    kernel32.QueryFullProcessImageNameW(
                        handle,
                        0,
                        buffer,
                        ctypes.byref(size),
                    )
                ):
                    return ""
                return Path(buffer.value).name.casefold()
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return ""
    def restore_easychange_foreground(
        self,
        attempts: int = 6,
        *,
        stable_checks: int = 2,
        stable_interval: float = 0.05,
        respect_user_state: bool = True,
        require_browser_foreground: bool = False,
    ) -> dict:
        hwnd = self._easychange_hwnd()
        user32 = _windows_user32()
        kernel32 = _windows_kernel32()
        if user32 is None or not hwnd:
            return {
                **self.easychange_focus_state(),
                "focus_restore_attempted": False,
                "focus_restore_attempts": 0,
            }

        last = self.easychange_focus_state()

        if respect_user_state:
            try:
                if bool(user32.IsIconic(hwnd)):
                    return {
                        **last,
                        "focus_restore_attempted": False,
                        "focus_restore_attempts": 0,
                        "focus_restore_skipped": "USER_MINIMIZED",
                        "user_focus_override": True,
                    }
            except Exception:
                pass

        if require_browser_foreground and respect_user_state:
            try:
                foreground = int(user32.GetForegroundWindow() or 0)
            except Exception:
                foreground = 0
            if foreground and foreground != hwnd:
                owner = self._foreground_process_name()
                if owner not in {"msedge.exe", "chrome.exe"}:
                    return {
                        **last,
                        "focus_restore_attempted": False,
                        "focus_restore_attempts": 0,
                        "focus_restore_skipped": "USER_SWITCHED_APPLICATION",
                        "user_focus_override": True,
                        "foreground_process": owner or None,
                    }

        if last.get("command_focus_confirmed"):
            return {
                **last,
                "focus_restore_attempted": False,
                "focus_restore_attempts": 0,
                "focus_restore_skipped": "ALREADY_FOREGROUND",
            }

        count = max(1, min(10, int(attempts or 1)))
        required_stable_checks = max(1, min(8, int(stable_checks or 1)))
        stable_interval = max(0.0, min(0.25, float(stable_interval or 0.0)))
        strategies: list[str] = []

        for index in range(count):
            foreground = 0
            foreground_thread = 0
            target_thread = 0
            current_thread = 0
            attached: list[tuple[int, int]] = []
            try:
                foreground = int(user32.GetForegroundWindow() or 0)
                if kernel32 is not None:
                    current_thread = int(kernel32.GetCurrentThreadId() or 0)
                if foreground:
                    foreground_thread = int(
                        user32.GetWindowThreadProcessId(foreground, None) or 0
                    )
                target_thread = int(
                    user32.GetWindowThreadProcessId(hwnd, None) or 0
                )

                for left, right in (
                    (current_thread, foreground_thread),
                    (current_thread, target_thread),
                ):
                    if left and right and left != right:
                        try:
                            if bool(user32.AttachThreadInput(left, right, True)):
                                attached.append((left, right))
                        except Exception:
                            pass

                if bool(user32.IsIconic(hwnd)):
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    strategies.append("SW_RESTORE")
                else:
                    user32.ShowWindow(hwnd, 5)  # SW_SHOW
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                user32.SetActiveWindow(hwnd)
                try:
                    user32.SetFocus(hwnd)
                except Exception:
                    pass
                strategies.append(
                    "ATTACH_THREAD_INPUT"
                    if attached else "DIRECT_FOREGROUND"
                )
            except Exception as exc:
                strategies.append(f"ERROR:{type(exc).__name__}")
            finally:
                for left, right in reversed(attached):
                    try:
                        user32.AttachThreadInput(left, right, False)
                    except Exception:
                        pass

            stable = 0
            for stable_index in range(required_stable_checks):
                last = self.easychange_focus_state()
                if not last.get("command_focus_confirmed"):
                    stable = 0
                    break
                stable += 1
                if stable < required_stable_checks and stable_interval > 0:
                    time.sleep(stable_interval)
            if stable >= required_stable_checks:
                return {
                    **last,
                    "focus_restore_attempted": True,
                    "focus_restore_attempts": index + 1,
                    "focus_restore_strategy": strategies[-1] if strategies else None,
                    "focus_restore_strategies": strategies,
                    "focus_restore_stable_checks": stable,
                    "focus_restore_required_stable_checks": required_stable_checks,
                    "foreground_before_restore": foreground or None,
                    "foreground_thread_id": foreground_thread or None,
                    "easychange_thread_id": target_thread or None,
                    "current_thread_id": current_thread or None,
                }
            time.sleep(max(0.04, stable_interval))

        return {
            **last,
            "focus_restore_attempted": True,
            "focus_restore_attempts": count,
            "focus_restore_strategy": strategies[-1] if strategies else None,
            "focus_restore_strategies": strategies,
            "focus_restore_stable_checks": 0,
            "focus_restore_required_stable_checks": required_stable_checks,
            "foreground_before_restore": foreground or None,
            "foreground_thread_id": foreground_thread or None,
            "easychange_thread_id": target_thread or None,
            "current_thread_id": current_thread or None,
        }

    @staticmethod
    def candidates() -> list[str]:
        """Return already-installed Edge/Chrome executables only, Edge first."""
        values: list[str] = []

        def add(value: str | None) -> None:
            if not value:
                return
            text = str(value)
            name = Path(text).name.casefold()
            if name not in {"msedge.exe", "msedge", "chrome.exe", "chrome"}:
                return
            if text not in values:
                values.append(text)

        for name in ("msedge", "msedge.exe"):
            add(shutil.which(name))
        if os.name == "nt":
            for root, suffix in (
                (os.environ.get("PROGRAMFILES(X86)"), "Microsoft/Edge/Application/msedge.exe"),
                (os.environ.get("PROGRAMFILES"), "Microsoft/Edge/Application/msedge.exe"),
                (os.environ.get("LOCALAPPDATA"), "Microsoft/Edge/Application/msedge.exe"),
            ):
                if root:
                    path = Path(root) / suffix
                    if path.is_file():
                        add(str(path))
        for name in ("chrome", "chrome.exe"):
            add(shutil.which(name))
        if os.name == "nt":
            for root, suffix in (
                (os.environ.get("PROGRAMFILES"), "Google/Chrome/Application/chrome.exe"),
                (os.environ.get("PROGRAMFILES(X86)"), "Google/Chrome/Application/chrome.exe"),
                (os.environ.get("LOCALAPPDATA"), "Google/Chrome/Application/chrome.exe"),
            ):
                if root:
                    path = Path(root) / suffix
                    if path.is_file():
                        add(str(path))
        return values

    @staticmethod
    def _browser_family(browser_path: str) -> str:
        name = Path(str(browser_path or "")).name.casefold()
        if name in {"msedge.exe", "msedge"}:
            return "edge"
        if name in {"chrome.exe", "chrome"}:
            return "chrome"
        raise BrowserError("ONLY_INSTALLED_EDGE_OR_CHROME_ALLOWED")

    def _persistent_profile_dir(self, browser_path: str) -> Path:
        family = self._browser_family(browser_path)
        root = (
            Path(os.environ.get("LOCALAPPDATA") or Path.home())
            / "EasyChange"
            / "BrowserProfiles"
            / family
        )
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _persistent_profile_candidates(
        self,
        browser_path: str,
    ) -> list[tuple[str, Path]]:
        """Predictable durable EasyChange-controlled profile pool.

        The historical primary path is preserved for compatibility. Fallback
        slots are stable siblings, never random/temp directories, and are not
        deleted on detach/restart.
        """
        family = self._browser_family(browser_path)
        primary = self._persistent_profile_dir(browser_path)
        root = primary.parent
        values = [
            ("primary", primary),
            ("fallback-1", root / f"{family}-fallback-1"),
            ("fallback-2", root / f"{family}-fallback-2"),
        ]
        for _slot, profile in values:
            profile.mkdir(parents=True, exist_ok=True)
        return values

    @staticmethod
    def _profile_path_redacted(profile: Path) -> str:
        return f"EasyChange/BrowserProfiles/{profile.name}"

    @staticmethod
    def _browser_process_present(browser_path: str) -> bool:
        if os.name != "nt":
            return False
        image = Path(str(browser_path or "")).name.casefold()
        if not image:
            return False
        try:
            import ctypes
            from ctypes import wintypes

            TH32CS_SNAPPROCESS = 0x00000002
            INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            kernel32 = ctypes.windll.kernel32
            snapshot = kernel32.CreateToolhelp32Snapshot(
                TH32CS_SNAPPROCESS,
                0,
            )
            if snapshot == INVALID_HANDLE_VALUE:
                return False
            try:
                entry = PROCESSENTRY32W()
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                if not kernel32.Process32FirstW(
                    snapshot,
                    ctypes.byref(entry),
                ):
                    return False
                while True:
                    if str(entry.szExeFile or "").casefold() == image:
                        return True
                    if not kernel32.Process32NextW(
                        snapshot,
                        ctypes.byref(entry),
                    ):
                        break
                return False
            finally:
                kernel32.CloseHandle(snapshot)
        except Exception:
            return False

    @staticmethod
    def _compact_bootstrap_diagnostics(value: dict | None) -> dict:
        source = dict(value or {})
        keys = (
            "browser",
            "browser_path",
            "bootstrap_strategy",
            "session_id",
            "profile_kind",
            "profile_path_redacted",
            "profile_persistent",
            "profile_slot",
            "cdp_address",
            "cdp_port_selected",
            "port",
            "pid",
            "browser_process_alive",
            "process_alive",
            "existing_browser_detected",
            "existing_browser_reused",
            "cdp_ready",
            "json_version_ready",
            "json_list_ready",
            "websocket_ready",
            "target_id",
            "fallback_attempted",
            "fallback_strategy",
            "fallback_success",
            "stage",
            "duration_ms",
            "no_startup_window",
            "background_target_created",
            "bootstrap_target_id",
            "secrets_exposed",
        )
        return {key: source.get(key) for key in keys if key in source}

    @staticmethod
    def _human_profile_dir(family: str) -> Path | None:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        if family == "edge":
            return Path(local) / "Microsoft" / "Edge" / "User Data"
        if family == "chrome":
            return Path(local) / "Google" / "Chrome" / "User Data"
        return None

    @staticmethod
    def _active_port(profile: Path) -> int:
        marker = profile / "DevToolsActivePort"
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
            value = int(lines[0])
            return value if 1 <= value <= 65535 else 0
        except (OSError, ValueError, IndexError):
            return 0

    def _discover_devtools_ports(self) -> list[dict]:
        values: list[dict] = []
        seen: set[int] = set()
        for family in ("edge", "chrome"):
            roots: list[tuple[str, Path, str | None]] = []
            human = self._human_profile_dir(family)
            if human is not None:
                roots.append(("existing_human", human, None))
            base = (
                Path(os.environ.get("LOCALAPPDATA") or Path.home())
                / "EasyChange" / "BrowserProfiles"
            )
            for slot, persistent in (
                ("primary", base / family),
                ("fallback-1", base / f"{family}-fallback-1"),
                ("fallback-2", base / f"{family}-fallback-2"),
            ):
                roots.append((
                    "easychange_persistent_nondefault",
                    persistent,
                    slot,
                ))
            for mode, root, profile_slot in roots:
                port = self._active_port(root)
                if not port or port in seen:
                    continue
                seen.add(port)
                values.append({
                    "browser": family,
                    "profile_mode": mode,
                    "profile_dir": str(root),
                    "profile_slot": profile_slot,
                    "port": port,
                })
        return values

    @staticmethod
    def _sanitize_url_for_diagnostics(url: str) -> str:
        try:
            parsed = urllib.parse.urlsplit(str(url or ""))
            if not parsed.scheme:
                return str(url or "")[:300]
            return urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, "", "")
            )[:500]
        except Exception:
            return "[UNPARSEABLE_URL]"

    def _safe_capture_tail(self, capture) -> str:
        if capture is None:
            return ""
        try:
            import re
            text = str(self._redact_snapshot_text(capture.tail()) or "")
            text = re.sub(
                r"(https?://[^\s?#]+)(?:\?[^\s#]*)?(?:#[^\s]*)?",
                lambda match: match.group(1),
                text,
                flags=re.I,
            )
            text = re.sub(
                r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+",
                "Bearer [REDACTED]",
                text,
            )
            text = re.sub(
                r"(?i)\b(token|password|api[_-]?key|authorization|cookie)\s*=\s*[^\s;]+",
                lambda match: f"{match.group(1)}=[REDACTED]",
                text,
            )
            return text[-4000:]
        except Exception:
            return ""

    def _bootstrap_diagnostics(
        self,
        *,
        browser: str,
        family: str,
        profile: Path,
        process=None,
        stdout_capture=None,
        stderr_capture=None,
        stage: str,
        port: int = 0,
        started_at: float,
        url: str = "",
        attempt: int = 1,
        json_version_available: bool = False,
        json_list_available: bool = False,
        target_available: bool = False,
        websocket_connected: bool = False,
        browser_get_version_ok: bool = False,
        runtime_evaluate_ok: bool = False,
    ) -> dict:
        return_code = None
        process_alive = None
        pid = None
        if process is not None:
            pid = int(getattr(process, "pid", 0) or 0) or None
            try:
                return_code = process.poll()
                process_alive = return_code is None
            except Exception:
                process_alive = None
        return {
            "browser": family,
            "browser_path": str(browser),
            "browser_process_started": process is not None,
            "browser_process_exited": bool(process is not None and process_alive is False),
            "browser_process_alive": process_alive,
            "pid": pid,
            "return_code": return_code,
            "cdp_address": "127.0.0.1",
            "cdp_port_requested": 0,
            "cdp_port_selected": int(port or 0) or None,
            "cdp_port_listening": bool(port and json_version_available),
            "json_version_available": bool(json_version_available),
            "json_list_available": bool(json_list_available),
            "target_available": bool(target_available),
            "websocket_connected": bool(websocket_connected),
            "browser_get_version_ok": bool(browser_get_version_ok),
            "runtime_evaluate_ok": bool(runtime_evaluate_ok),
            "profile_mode": "easychange_persistent_nondefault",
            "profile_persistent": True,
            "profile_kind": (
                "easychange_durable_primary"
                if profile.name == family
                else "easychange_durable_fallback"
            ),
            "profile_name": profile.name,
            "profile_path_redacted": self._profile_path_redacted(profile),
            "bootstrap_strategy": "installed_browser_durable_profile_cdp",
            "existing_browser_detected": False,
            "existing_browser_reused": False,
            "cdp_ready": bool(
                port
                and json_version_available
                and json_list_available
                and target_available
                and websocket_connected
                and browser_get_version_ok
                and runtime_evaluate_ok
            ),
            "json_version_ready": bool(json_version_available),
            "json_list_ready": bool(json_list_available),
            "websocket_ready": bool(websocket_connected),
            "fallback_attempted": False,
            "fallback_strategy": None,
            "fallback_success": False,
            "url": self._sanitize_url_for_diagnostics(url),
            "stage": str(stage),
            "attempt": int(attempt),
            "duration_ms": round((time.monotonic() - started_at) * 1000, 2),
            "stdout_tail": self._safe_capture_tail(stdout_capture),
            "stderr_tail": self._safe_capture_tail(stderr_capture),
            "secrets_exposed": False,
        }

    @staticmethod
    def _safe_id(value: str) -> str:
        raw = str(value or "default")
        safe = "".join(ch for ch in raw if ch.isalnum() or ch in "_-")
        if not safe or safe != raw:
            raise ValueError("INVALID_BROWSER_SESSION_ID")
        return safe

    @staticmethod
    def _http_json(url: str, method: str = "GET", timeout: float = 3.0):
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").casefold()
        if host not in {"localhost", "::1"} and not host.startswith("127."):
            raise BrowserError("CDP_NON_LOOPBACK_FORBIDDEN")
        request = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(1024 * 1024).decode("utf-8"))

    def _wait_port(self, profile: Path, timeout: float) -> int:
        marker = profile / "DevToolsActivePort"
        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            try:
                if marker.exists():
                    value = int(marker.read_text(encoding="utf-8").splitlines()[0])
                    if value > 0:
                        return value
            except (OSError, ValueError, IndexError):
                pass
            time.sleep(0.05)
        raise TimeoutError("BROWSER_CDP_START_TIMEOUT")

    def _targets(self, port: int) -> list[dict]:
        values = self._http_json(f"http://127.0.0.1:{int(port)}/json/list")
        return [
            dict(value)
            for value in (values if isinstance(values, list) else [])
            if isinstance(value, dict)
            and value.get("type") == "page"
            and value.get("webSocketDebuggerUrl")
        ]

    def _ensure_background_page_target(
        self,
        port: int,
        url: str,
        *,
        timeout: float = 5.0,
    ) -> tuple[str, bool]:
        """Ensure a page target exists without activating a browser window.

        Fresh Edge/Chrome bootstrap uses --no-startup-window so Windows keeps
        EasyChange in the foreground. We connect only to the loopback browser
        websocket and create the first page as a background target.
        """
        targets = self._targets(port)
        if targets:
            return str(targets[0].get("id") or ""), False

        version = self._http_json(
            f"http://127.0.0.1:{int(port)}/json/version",
            timeout=max(0.5, min(3.0, float(timeout))),
        )
        browser_ws = str(
            (version.get("webSocketDebuggerUrl") or "")
            if isinstance(version, dict)
            else ""
        )
        if not browser_ws:
            raise BrowserError("CDP_BROWSER_WEBSOCKET_MISSING")

        browser_cdp = _WebSocket(
            browser_ws,
            timeout=max(1.0, min(5.0, float(timeout))),
        )
        try:
            created = browser_cdp.call(
                "Target.createTarget",
                {
                    "url": str(url or "about:blank"),
                    "background": True,
                },
                timeout=max(1.0, min(5.0, float(timeout))),
            )
        finally:
            browser_cdp.close()

        target_id = str((created or {}).get("targetId") or "")
        if not target_id:
            raise BrowserError("CDP_PAGE_TARGET_NOT_CREATED")

        deadline = time.monotonic() + max(0.5, min(5.0, float(timeout)))
        while time.monotonic() < deadline:
            target = next(
                (
                    item for item in self._targets(port)
                    if str(item.get("id") or "") == target_id
                ),
                None,
            )
            if target is not None:
                return target_id, True
            time.sleep(0.03)
        raise BrowserError("CDP_PAGE_TARGET_NOT_AVAILABLE_AFTER_CREATE")

    def _candidate_ports(self, explicit_port: int = 0) -> list[int]:
        if explicit_port:
            value = int(explicit_port)
            if not 1 <= value <= 65535:
                raise ValueError("INVALID_CDP_PORT")
            return [value]
        values: list[int] = []
        def add(value) -> None:
            try:
                number = int(value or 0)
            except (TypeError, ValueError):
                return
            if 1 <= number <= 65535 and number not in values:
                values.append(number)
        for item in self._discover_devtools_ports():
            add(item.get("port"))
        for raw in str(os.environ.get("EASYCHANGE_CDP_PORTS") or "").split(","):
            add(raw.strip())
        for value in range(9222, 9233):
            add(value)
        return values

    def attach(
        self,
        session_id: str = "default",
        *,
        port: int = 0,
        target_id: str = "",
        url_contains: str = "",
        timeout: float = 5.0,
    ) -> dict:
        key = self._safe_id(session_id)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                try:
                    state = self.state(key)
                except Exception:
                    # Only an actually stale/dead session is discarded. Target
                    # selection errors below are caller-visible and must not
                    # silently tear down a healthy session and attach elsewhere.
                    try:
                        existing.cdp.close()
                        existing.browser_cdp.close()
                    except Exception:
                        pass
                    self._sessions.pop(key, None)
                else:
                    if state.get("alive"):
                        if target_id and str(existing.target_id) != str(target_id):
                            self._connect_target(key, str(target_id))
                        elif url_contains and url_contains not in str(state.get("url") or ""):
                            candidate = next(
                                (
                                    item for item in (self.tabs(key).get("tabs") or [])
                                    if url_contains in str(item.get("url") or "")
                                ),
                                None,
                            )
                            if candidate:
                                self._connect_target(key, str(candidate["target_id"]))
                            else:
                                raise LookupError(
                                    f"BROWSER_TAB_NOT_FOUND_FOR_URL:{url_contains[:300]}"
                                )
                        return {**self.state(key), "reused": True}

            discovered = [] if port else self._discover_devtools_ports()
            discovered_by_port = {int(item["port"]): item for item in discovered}
            ports: list[int] = []
            for item in discovered:
                value = int(item["port"])
                if value not in ports:
                    ports.append(value)
            for value in self._candidate_ports(int(port or 0)):
                if value not in ports:
                    ports.append(value)

            errors: list[dict] = []
            deadline = time.monotonic() + max(0.5, min(15.0, float(timeout)))
            for candidate_port in ports:
                remaining = max(0.2, deadline - time.monotonic())
                if remaining <= 0:
                    break
                stage = "json_version"
                try:
                    version = self._http_json(
                        f"http://127.0.0.1:{candidate_port}/json/version",
                        timeout=min(1.0, remaining),
                    )
                    product = str(version.get("Browser") if isinstance(version, dict) else "")
                    normalized = product.casefold()
                    if "chromium" in normalized or not any(
                        token in normalized for token in ("chrome", "edg", "edge")
                    ):
                        raise BrowserError("CDP_ENDPOINT_IS_NOT_EDGE_OR_CHROME")
                    browser_ws = str(
                        (version.get("webSocketDebuggerUrl") or "")
                        if isinstance(version, dict)
                        else ""
                    )
                    if not browser_ws:
                        raise BrowserError("CDP_BROWSER_WEBSOCKET_MISSING")

                    stage = "json_list"
                    targets = self._targets(candidate_port)
                    if target_id:
                        target = next(
                            (item for item in targets if str(item.get("id") or "") == str(target_id)),
                            None,
                        )
                    elif url_contains:
                        target = next(
                            (item for item in targets if url_contains in str(item.get("url") or "")),
                            None,
                        )
                    else:
                        target = next(
                            (
                                item for item in targets
                                if str(item.get("url") or "").startswith(("http://", "https://"))
                            ),
                            None,
                        ) or (targets[0] if targets else None)
                    if target is None:
                        raise BrowserError("CDP_PAGE_TARGET_NOT_FOUND")

                    stage = "websocket"
                    browser_cdp = _WebSocket(browser_ws, timeout=max(1.0, min(5.0, remaining)))
                    ws = str(target["webSocketDebuggerUrl"])
                    cdp = _WebSocket(ws, timeout=max(1.0, min(5.0, remaining)))
                    try:
                        stage = "browser_get_version"
                        browser_version = browser_cdp.call(
                            "Browser.getVersion",
                            timeout=min(3.0, max(0.5, remaining)),
                        )
                        product_check = str(
                            (browser_version or {}).get("product") or product
                        ).casefold()
                        if "chromium" in product_check or not any(
                            token in product_check for token in ("chrome", "edg", "edge")
                        ):
                            raise BrowserError("CDP_BROWSER_GET_VERSION_UNSUPPORTED")
                        for domain in ("Page.enable", "Runtime.enable", "DOM.enable"):
                            cdp.call(domain, timeout=min(3.0, max(0.5, remaining)))
                        stage = "runtime_evaluate"
                        evaluated = cdp.call(
                            "Runtime.evaluate",
                            {"expression": "document.title", "returnByValue": True},
                            timeout=min(3.0, max(0.5, remaining)),
                        )
                        if not isinstance(evaluated, dict) or evaluated.get("exceptionDetails"):
                            raise BrowserError("CDP_RUNTIME_EVALUATE_FAILED")
                        stage = "second_cdp_operation"
                        second = cdp.call(
                            "Runtime.evaluate",
                            {"expression": "location.href", "returnByValue": True},
                            timeout=min(3.0, max(0.5, remaining)),
                        )
                        if not isinstance(second, dict) or second.get("exceptionDetails"):
                            raise BrowserError("CDP_SECOND_OPERATION_FAILED")
                    except Exception:
                        cdp.close()
                        browser_cdp.close()
                        raise

                    marker = discovered_by_port.get(candidate_port)
                    profile_mode = (
                        str(marker.get("profile_mode"))
                        if marker else "existing_human"
                    )
                    profile_dir = (
                        Path(str(marker.get("profile_dir")))
                        if marker and marker.get("profile_dir") else None
                    )
                    profile_slot = (
                        str(marker.get("profile_slot") or "") or None
                        if marker else None
                    )
                    self._sessions[key] = BrowserSession(
                        session_id=key,
                        port=int(candidate_port),
                        target_id=str(target.get("id") or ""),
                        websocket_url=ws,
                        cdp=cdp,
                        browser_cdp=browser_cdp,
                        attached_at=time.time(),
                        browser_product=product,
                        profile_mode=profile_mode,
                        profile_dir=profile_dir,
                        profile_slot=profile_slot,
                    )
                    return {
                        **self.state(key),
                        "reused": False,
                        "json_version_available": True,
                        "json_list_available": True,
                        "target_available": True,
                        "websocket_connected": True,
                        "browser_get_version_ok": True,
                        "runtime_evaluate_ok": True,
                        "second_cdp_operation_ok": True,
                        "attach_only": True,
                        "existing_human_session": profile_mode == "existing_human",
                        "browser_launched": False,
                        "profile_created": False,
                        "install_required": False,
                        "discovered_via_devtools_active_port": marker is not None,
                    }
                except Exception as exc:
                    detail = str(exc)[:160]
                    code = (
                        detail.split(":", 1)[0]
                        if detail.startswith("CDP_")
                        else type(exc).__name__
                    )
                    errors.append({
                        "port": candidate_port,
                        "stage": stage,
                        "error": type(exc).__name__,
                        "code": code,
                        "detail": detail,
                    })
            diagnostics = {
                "localhost_only": True,
                "ports_checked": ports,
                "devtools_active_ports": [
                    {
                        "browser": item.get("browser"),
                        "profile_mode": item.get("profile_mode"),
                        "port": item.get("port"),
                    }
                    for item in discovered
                ],
                "attempts": errors[-12:],
            }
            if port and errors:
                raise BrowserBootstrapError(
                    str(errors[-1].get("code") or "CDP_EXISTING_SESSION_NOT_AVAILABLE"),
                    diagnostics,
                )
            raise BrowserBootstrapError(
                "CDP_EXISTING_SESSION_NOT_AVAILABLE",
                diagnostics,
            )

    def _launch_controlled_profile(
        self,
        session_id: str,
        *,
        browser: str,
        profile_slot: str,
        profile: Path,
        url: str,
        visible: bool,
        timeout: float,
        attach_error: str,
        fallback_attempted: bool,
        fallback_strategy: str | None,
        existing_browser_detected: bool,
    ) -> dict:
        family = self._browser_family(browser)
        profile.mkdir(parents=True, exist_ok=True)
        profile_existed = any(profile.iterdir())
        started_at = time.monotonic()

        prior_port = self._active_port(profile)
        if prior_port:
            try:
                self.attach(
                    session_id,
                    port=prior_port,
                    timeout=min(3.0, timeout),
                )
                session = self._session(session_id)
                session.profile_dir = profile
                session.profile_mode = "easychange_persistent_nondefault"
                session.profile_slot = profile_slot
                session.browser_path = browser
                if url and url != "about:blank":
                    self.navigate(session_id, url, timeout)
                if visible:
                    try:
                        self.show(session_id)
                    except Exception:
                        pass
                state = self.state(session_id)
                bootstrap = {
                    "browser": family,
                    "browser_path": browser,
                    "bootstrap_strategy": "reuse_easychange_durable_profile_cdp",
                    "session_id": self._safe_id(session_id),
                    "profile_kind": (
                        "easychange_durable_primary"
                        if profile_slot == "primary"
                        else "easychange_durable_fallback"
                    ),
                    "profile_path_redacted": self._profile_path_redacted(profile),
                    "profile_persistent": True,
                    "profile_slot": profile_slot,
                    "port": int(prior_port),
                    "pid": state.get("pid"),
                    "process_alive": state.get("pid_alive"),
                    "existing_browser_detected": bool(existing_browser_detected),
                    "existing_browser_reused": True,
                    "cdp_ready": bool(state.get("cdp_alive")),
                    "json_version_ready": True,
                    "json_list_ready": True,
                    "websocket_ready": True,
                    "target_id": state.get("target_id"),
                    "fallback_attempted": bool(fallback_attempted),
                    "fallback_strategy": fallback_strategy,
                    "fallback_success": bool(fallback_attempted),
                    "secrets_exposed": False,
                }
                session.launch_diagnostics = bootstrap
                return {
                    **state,
                    "attach_first": True,
                    "persistent_profile_reused": True,
                    "browser_launched": False,
                    "profile_created": False,
                    "profile_persistent": True,
                    "profile_mode": session.profile_mode,
                    "profile_slot": profile_slot,
                    "profile_path_redacted": self._profile_path_redacted(profile),
                    "human_profile_reused": False,
                    "default_profile_preserved": True,
                    "install_required": False,
                    "fallback_attempted": bool(fallback_attempted),
                    "fallback_strategy": fallback_strategy,
                    "fallback_success": bool(fallback_attempted),
                    "bootstrap": self._compact_bootstrap_diagnostics(bootstrap),
                }
            except Exception:
                try:
                    (profile / "DevToolsActivePort").unlink(missing_ok=True)
                except OSError:
                    pass

        argv = [
            browser,
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-startup-window",
        ]
        safe_argv = list(argv)
        try:
            process = subprocess.Popen(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            diagnostics = self._bootstrap_diagnostics(
                browser=browser,
                family=family,
                profile=profile,
                stage="browser_process_start",
                started_at=started_at,
                url=url,
            )
            diagnostics.update({
                "argv": safe_argv,
                "launch_error": type(exc).__name__,
                "session_id": self._safe_id(session_id),
                "profile_slot": profile_slot,
                "fallback_attempted": bool(fallback_attempted),
                "fallback_strategy": fallback_strategy,
            })
            raise BrowserBootstrapError(
                "CDP_BROWSER_PROCESS_NOT_STARTED",
                diagnostics,
            ) from exc

        stdout_capture = _BoundedProcessCapture(process.stdout) if process.stdout else None
        stderr_capture = _BoundedProcessCapture(process.stderr) if process.stderr else None
        deadline = time.monotonic() + max(1.0, min(20.0, float(timeout)))
        port = 0
        last_error = ""
        attempt = 0

        while time.monotonic() < deadline:
            attempt += 1
            port = self._active_port(profile)
            if port:
                try:
                    remaining = min(5.0, max(0.5, deadline - time.monotonic()))
                    bootstrap_target_id, background_target_created = (
                        self._ensure_background_page_target(
                            port,
                            str(url or "about:blank"),
                            timeout=remaining,
                        )
                    )
                    self.attach(
                        session_id,
                        port=port,
                        target_id=bootstrap_target_id,
                        timeout=min(3.0, remaining),
                    )
                    session = self._session(session_id)
                    observed_product = str(session.browser_product or "").casefold()
                    expected_product = (
                        ("edg" in observed_product or "edge" in observed_product)
                        if family == "edge"
                        else (
                            "chrome" in observed_product
                            and "edg" not in observed_product
                            and "edge" not in observed_product
                        )
                    )
                    if not expected_product:
                        try:
                            session.cdp.close()
                            session.browser_cdp.close()
                        finally:
                            self._sessions.pop(self._safe_id(session_id), None)
                        raise BrowserError("CDP_BROWSER_PRODUCT_MISMATCH")

                    # If a page existed already, explicitly navigate it. A newly
                    # created background target already received the requested URL.
                    if (
                        not background_target_created
                        and url
                        and url != "about:blank"
                    ):
                        self.navigate(session_id, url, timeout)

                    session.managed_process = True
                    session.pid = int(process.pid)
                    session.launch_pid = int(process.pid)
                    session.browser_path = browser
                    session.profile_dir = profile
                    session.profile_mode = "easychange_persistent_nondefault"
                    session.profile_slot = profile_slot
                    session.stdout_capture = stdout_capture
                    session.stderr_capture = stderr_capture
                    session.process = process
                    if visible:
                        try:
                            self.show(session_id)
                        except Exception:
                            pass
                    else:
                        try:
                            self.hide(session_id)
                        except Exception:
                            pass

                    diagnostics = self._bootstrap_diagnostics(
                        browser=browser,
                        family=family,
                        profile=profile,
                        process=process,
                        stdout_capture=stdout_capture,
                        stderr_capture=stderr_capture,
                        stage="ready",
                        port=port,
                        started_at=started_at,
                        url=url,
                        attempt=attempt,
                        json_version_available=True,
                        json_list_available=True,
                        target_available=True,
                        websocket_connected=True,
                        browser_get_version_ok=True,
                        runtime_evaluate_ok=True,
                    )
                    diagnostics.update({
                        "argv": safe_argv,
                        "no_startup_window": True,
                        "background_target_created": bool(
                            background_target_created
                        ),
                        "bootstrap_target_id": bootstrap_target_id,
                        "target_id": session.target_id,
                        "session_id": self._safe_id(session_id),
                        "profile_slot": profile_slot,
                        "port": int(port),
                        "process_alive": True,
                        "existing_browser_detected": bool(existing_browser_detected),
                        "existing_browser_reused": False,
                        "fallback_attempted": bool(fallback_attempted),
                        "fallback_strategy": fallback_strategy,
                        "fallback_success": bool(fallback_attempted),
                    })
                    session.launch_diagnostics = diagnostics
                    return {
                        **self.state(session_id),
                        "attach_first": True,
                        "browser_launched": True,
                        "browser_launch_pid": int(process.pid),
                        "browser_path": browser,
                        "profile_created": not profile_existed,
                        "profile_persistent": True,
                        "profile_mode": session.profile_mode,
                        "profile_slot": profile_slot,
                        "profile_path_redacted": self._profile_path_redacted(profile),
                        "human_profile_reused": False,
                        "default_profile_preserved": True,
                        "install_required": False,
                        "fallback_attempted": bool(fallback_attempted),
                        "fallback_strategy": fallback_strategy,
                        "fallback_success": bool(fallback_attempted),
                        "bootstrap": self._compact_bootstrap_diagnostics(diagnostics),
                    }
                except Exception as exc:
                    last_error = f"{type(exc).__name__}:{str(exc)[:500]}"

            try:
                return_code = process.poll()
            except Exception:
                return_code = None
            elapsed = time.monotonic() - started_at

            if return_code is not None and int(return_code) != 0:
                diagnostics = self._bootstrap_diagnostics(
                    browser=browser,
                    family=family,
                    profile=profile,
                    process=process,
                    stdout_capture=stdout_capture,
                    stderr_capture=stderr_capture,
                    stage="process_exited_before_cdp",
                    started_at=started_at,
                    url=url,
                    attempt=attempt,
                )
                diagnostics.update({
                    "argv": safe_argv,
                    "attach_error": attach_error,
                    "session_id": self._safe_id(session_id),
                    "profile_slot": profile_slot,
                    "fallback_attempted": bool(fallback_attempted),
                    "fallback_strategy": fallback_strategy,
                })
                raise BrowserBootstrapError(
                    "CDP_BROWSER_PROCESS_EXITED",
                    diagnostics,
                )

            if return_code == 0 and not port and elapsed >= 0.8:
                diagnostics = self._bootstrap_diagnostics(
                    browser=browser,
                    family=family,
                    profile=profile,
                    process=process,
                    stdout_capture=stdout_capture,
                    stderr_capture=stderr_capture,
                    stage="existing_browser_reused_without_debugging",
                    started_at=started_at,
                    url=url,
                    attempt=attempt,
                )
                diagnostics.update({
                    "argv": safe_argv,
                    "attach_error": attach_error,
                    "session_id": self._safe_id(session_id),
                    "profile_slot": profile_slot,
                    "existing_browser_detected": True,
                    "existing_browser_reused": True,
                    "fallback_attempted": bool(fallback_attempted),
                    "fallback_strategy": fallback_strategy,
                })
                raise BrowserBootstrapError(
                    "CDP_EXISTING_BROWSER_REUSED_WITHOUT_DEBUGGING",
                    diagnostics,
                )
            time.sleep(0.05)

        diagnostics = self._bootstrap_diagnostics(
            browser=browser,
            family=family,
            profile=profile,
            process=process,
            stdout_capture=stdout_capture,
            stderr_capture=stderr_capture,
            stage="bootstrap_timeout",
            port=port,
            started_at=started_at,
            url=url,
            attempt=attempt,
        )
        diagnostics.update({
            "argv": safe_argv,
            "attach_error": attach_error,
            "last_cdp_error": last_error,
            "session_id": self._safe_id(session_id),
            "profile_slot": profile_slot,
            "fallback_attempted": bool(fallback_attempted),
            "fallback_strategy": fallback_strategy,
        })
        code = "CDP_BOOTSTRAP_TIMEOUT" if port else "CDP_PORT_NOT_OPEN"
        raise BrowserBootstrapError(code, diagnostics)

    def start(
        self,
        session_id: str = "default",
        *,
        url: str = "about:blank",
        headless: bool = False,
        visible: bool = True,
        width: int = 1440,
        height: int = 1000,
        timeout: float = 10.0,
        browser_path: str = "",
    ) -> dict:
        del width, height
        if headless:
            raise BrowserError("BROWSER_HEADLESS_FORBIDDEN")

        # First choice is always attach-only reuse of any already exposed local
        # Edge/Chrome CDP endpoint, including a prior EasyChange-controlled
        # durable profile after an EasyChange restart.
        try:
            attached = self.attach(session_id, timeout=min(5.0, timeout))
            target_strategy = "attached_target"
            if url and url != "about:blank":
                profile_mode = str(attached.get("profile_mode") or "")
                if profile_mode == "existing_human":
                    created = self.new_tab(
                        session_id,
                        url=str(url),
                        activate=True,
                    )
                    attached = self.state(session_id)
                    target_strategy = "new_tab_preserve_human_target"
                    attached["easychange_created_target_id"] = created.get("target_id")
                else:
                    self.navigate(session_id, url, timeout)
                    attached = self.state(session_id)
                    target_strategy = "navigate_easychange_controlled_target"
            if visible:
                try:
                    self.show(session_id)
                except Exception:
                    pass
            return {
                **attached,
                "attach_first": True,
                "target_strategy": target_strategy,
                "browser_launched": False,
                "profile_created": False,
                "profile_persistent": True,
                "profile_mode": attached.get(
                    "profile_mode",
                    "existing_human",
                ),
                "human_profile_reused": (
                    attached.get("profile_mode") == "existing_human"
                ),
                "install_required": False,
                "fallback_attempted": False,
                "fallback_strategy": None,
                "fallback_success": False,
            }
        except Exception as attach_exc:
            attach_error = (
                f"{type(attach_exc).__name__}:{str(attach_exc)[:500]}"
            )

        installed = self.candidates()
        requested = str(browser_path or "").strip()
        if requested:
            requested_path = Path(requested)
            allowed = {
                str(Path(value).resolve()).casefold()
                for value in installed
            }
            if (
                not requested_path.is_file()
                or str(requested_path.resolve()).casefold() not in allowed
            ):
                raise BrowserError("ONLY_INSTALLED_EDGE_OR_CHROME_ALLOWED")
            browsers = [str(requested_path)]
        else:
            browsers = list(installed)
        if not browsers:
            raise FileNotFoundError("NO_INSTALLED_EDGE_OR_CHROME_FOUND")

        failures: list[dict] = []
        last_exc: BrowserBootstrapError | None = None
        existing_reuse_seen = False

        for browser_index, browser in enumerate(browsers):
            existing_browser_detected = self._browser_process_present(browser)
            for profile_index, (profile_slot, profile) in enumerate(
                self._persistent_profile_candidates(browser)
            ):
                # Reaching a controlled EasyChange profile already means the
                # attach-first path could not use an existing CDP session.
                fallback_attempted = True
                if browser_index > 0:
                    fallback_strategy = "alternate_installed_browser"
                elif profile_index > 0 or existing_reuse_seen:
                    fallback_strategy = "durable_profile_rotation"
                else:
                    fallback_strategy = "durable_controlled_profile"
                try:
                    result = self._launch_controlled_profile(
                        session_id,
                        browser=browser,
                        profile_slot=profile_slot,
                        profile=profile,
                        url=url,
                        visible=visible,
                        timeout=timeout,
                        attach_error=attach_error,
                        fallback_attempted=fallback_attempted,
                        fallback_strategy=fallback_strategy,
                        existing_browser_detected=existing_browser_detected,
                    )
                    result["fallback_attempted"] = True
                    result["fallback_success"] = True
                    result["fallback_strategy"] = fallback_strategy
                    bootstrap = result.get("bootstrap")
                    if isinstance(bootstrap, dict):
                        bootstrap["fallback_attempted"] = True
                        bootstrap["fallback_success"] = True
                        bootstrap["fallback_strategy"] = fallback_strategy
                        if failures:
                            bootstrap["fallback_attempts"] = list(failures)
                    return result
                except BrowserBootstrapError as exc:
                    last_exc = exc
                    code = str(exc.code or type(exc).__name__)
                    if code == "CDP_EXISTING_BROWSER_REUSED_WITHOUT_DEBUGGING":
                        existing_reuse_seen = True
                    diag = exc.diagnostics if isinstance(exc.diagnostics, dict) else {}
                    failures.append({
                        "browser": self._browser_family(browser),
                        "profile_slot": profile_slot,
                        "profile_path_redacted": self._profile_path_redacted(profile),
                        "code": code,
                        "stage": diag.get("stage"),
                        "existing_browser_reused": bool(
                            diag.get("existing_browser_reused")
                        ),
                        "cdp_ready": bool(diag.get("cdp_ready")),
                    })
                    # browser_open owns fallback. Never stop/kill the human or
                    # prior browser process; simply try the next durable slot.
                    continue

        if last_exc is not None:
            diagnostics = dict(last_exc.diagnostics or {})
            diagnostics.update({
                "fallback_attempted": len(failures) > 1,
                "fallback_success": False,
                "fallback_strategy": (
                    "durable_profile_rotation"
                    if any(
                        item.get("profile_slot") != "primary"
                        for item in failures
                    )
                    else None
                ),
                "fallback_attempts": failures,
                "existing_browser_reused": bool(existing_reuse_seen),
            })
            raise BrowserBootstrapError(last_exc.code, diagnostics)
        raise BrowserError("CDP_BROWSER_OPEN_FALLBACK_EXHAUSTED")

    def _session(self, session_id: str) -> BrowserSession:
        key = self._safe_id(session_id)
        value = self._sessions.get(key)
        if value is None:
            raise LookupError(f"BROWSER_SESSION_NOT_FOUND:{key}")
        return value

    def stop(self, session_id: str = "default", *, preserve_profile: bool = False) -> dict:
            del preserve_profile
            key = self._safe_id(session_id)
            with self._lock:
                session = self._sessions.pop(key, None)
                if session is None:
                    raise LookupError(f"BROWSER_SESSION_NOT_FOUND:{key}")
                for channel in (session.cdp, session.browser_cdp):
                    try:
                        channel.close()
                    except Exception:
                        pass
                return {
                    "session_id": key,
                    "state": "DETACHED",
                    "browser_closed": False,
                    "process_stopped": False,
                    "profile_deleted": False,
                    "existing_human_session_preserved": True,
                }

    def _evaluate(self, session_id: str, expression: str, timeout: float = 10.0):
        result = self._session(session_id).cdp.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        exception = result.get("exceptionDetails")
        if isinstance(exception, dict):
            raise BrowserError(
                "BROWSER_EVALUATION_FAILED:"
                + str(exception.get("text") or "JavaScript exception")[:500]
            )
        value = result.get("result")
        return value.get("value") if isinstance(value, dict) else None

    def url(self, session_id: str = "default") -> str:
        return str(self._evaluate(session_id, "location.href") or "")

    def title(self, session_id: str = "default") -> str:
        return str(self._evaluate(session_id, "document.title") or "")

    def state(self, session_id: str = "default") -> dict:
        session = self._session(session_id)
        cdp_alive = False
        current_url = None
        current_title = None
        cdp_error = None
        product = session.browser_product
        try:
            current_url = self.url(session_id)
            current_title = self.title(session_id)
            version = session.browser_cdp.call("Browser.getVersion", timeout=3.0)
            if isinstance(version, dict):
                product = str(version.get("product") or product)
            cdp_alive = True
        except Exception as exc:
            cdp_error = f"{type(exc).__name__}:{str(exc)[:300]}"
        pid_alive = None
        if session.process is not None:
            try:
                pid_alive = session.process.poll() is None
            except Exception:
                pid_alive = None
        value = {
            "session_id": session.session_id,
            "port": session.port,
            "target_id": session.target_id,
            "pid": session.pid,
            "pid_alive": pid_alive,
            "cdp_alive": cdp_alive,
            "alive": cdp_alive,
            "browser": product,
            "headless": False,
            "attached_at": session.attached_at,
            "launch_mode": (
                "easychange_persistent_cdp"
                if session.profile_mode == "easychange_persistent_nondefault"
                else "attach_existing"
            ),
            "attach_only": session.profile_mode == "existing_human",
            "managed_process": bool(session.managed_process),
            "existing_human_session": session.profile_mode == "existing_human",
            "cdp_transport": "127.0.0.1",
            "cdp_loopback_only": True,
            "install_required": False,
            "browser_launched": bool(session.launch_pid),
            "profile_created": False,
            "profile_persistent": True,
            "profile_mode": session.profile_mode,
            "profile_slot": session.profile_slot,
            "profile_path_redacted": (
                self._profile_path_redacted(session.profile_dir)
                if session.profile_dir is not None
                else None
            ),
            "browser_path": session.browser_path or None,
            "bootstrap": self._compact_bootstrap_diagnostics(
                session.launch_diagnostics
            ),
        }
        if cdp_alive:
            value["url"] = current_url
            value["title"] = current_title
        if cdp_error:
            value["cdp_error"] = cdp_error
        return value

    def _window_info(self, session_id: str) -> dict:
        session = self._session(session_id)
        if session.headless:
            return {"headless": True, "visible": False, "state": "headless"}
        info = session.browser_cdp.call(
            "Browser.getWindowForTarget",
            {"targetId": session.target_id},
            timeout=5.0,
        )
        window_id = int(info.get("windowId"))
        bounds_result = session.browser_cdp.call(
            "Browser.getWindowBounds",
            {"windowId": window_id},
            timeout=5.0,
        )
        bounds = bounds_result.get("bounds") if isinstance(bounds_result, dict) else {}
        state = str((bounds or {}).get("windowState") or "normal")
        return {
            "headless": False,
            "window_id": window_id,
            "state": state,
            "visible": state != "minimized",
            "bounds": bounds or {},
        }

    def set_visibility(self, session_id: str, visible: bool) -> dict:
        session = self._session(session_id)
        if session.headless:
            return {
                "session_id": session_id,
                "headless": True,
                "visible": False,
                "state": "headless",
                "changed": False,
            }
        info = session.browser_cdp.call(
            "Browser.getWindowForTarget",
            {"targetId": session.target_id},
            timeout=5.0,
        )
        window_id = int(info.get("windowId"))
        session.browser_cdp.call(
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {"windowState": "normal" if visible else "minimized"},
            },
            timeout=5.0,
        )
        time.sleep(0.04)
        return {
            "session_id": session_id,
            **self._window_info(session_id),
            "changed": True,
        }

    def show(self, session_id: str = "default") -> dict:
        return self.set_visibility(session_id, True)

    def hide(self, session_id: str = "default") -> dict:
        return self.set_visibility(session_id, False)

    def position(self, session_id: str = "default") -> dict:
        value = self._evaluate(
            session_id,
            """(() => {
const d=document.documentElement,b=document.body;
const totalWidth=Math.max(d.scrollWidth,d.offsetWidth,d.clientWidth,b?b.scrollWidth:0,b?b.offsetWidth:0);
const totalHeight=Math.max(d.scrollHeight,d.offsetHeight,d.clientHeight,b?b.scrollHeight:0,b?b.offsetHeight:0);
return {url:location.href,title:document.title,scrollX:window.scrollX,scrollY:window.scrollY,viewportWidth:window.innerWidth,viewportHeight:window.innerHeight,totalWidth,totalHeight,atTop:window.scrollY<=1,atBottom:(window.scrollY+window.innerHeight)>=totalHeight-2};
})()""",
        )
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _redact_snapshot_text(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        import re
        text = re.sub(
            r"(?i)(authorization)(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+",
            r"\1\2[REDACTED]",
            value,
        )
        text = re.sub(
            r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)(\s*[:=]\s*)[^\s,;]+",
            r"\1\2[REDACTED]",
            text,
        )
        return re.sub(
            r"(?i)bearer\s+[A-Za-z0-9._~+/-]{12,}",
            "Bearer [REDACTED]",
            text,
        )

    def snapshot(
        self,
        session_id: str = "default",
        *,
        max_elements: int = 80,
        max_text_blocks: int = 40,
    ) -> dict:
        max_elements = max(1, min(200, int(max_elements)))
        max_text_blocks = max(0, min(100, int(max_text_blocks)))
        script = """(() => {
const visible=(e)=>!!(e&&(e.offsetWidth||e.offsetHeight||e.getClientRects().length)&&getComputedStyle(e).visibility!=='hidden'&&getComputedStyle(e).display!=='none');
const role=(e)=>{if(e.getAttribute('role'))return e.getAttribute('role');if(e.tagName==='BUTTON')return 'button';if(e.tagName==='A')return 'link';if(e.tagName==='TEXTAREA')return 'textbox';if(e.tagName==='SELECT')return 'combobox';if(e.tagName==='INPUT'){const t=(e.type||'text').toLowerCase();if(t==='checkbox')return 'checkbox';if(t==='radio')return 'radio';if(['button','submit','reset'].includes(t))return 'button';if(t==='range')return 'slider';return 'textbox';}return e.tagName.toLowerCase();};
const name=(e)=>e.getAttribute('aria-label')||e.getAttribute('title')||e.getAttribute('placeholder')||(e.innerText||e.textContent||'').trim();
const css=(e)=>{if(e.id)return '#'+CSS.escape(e.id);let parts=[];while(e&&e.nodeType===1&&e!==document.body){let part=e.tagName.toLowerCase();let same=[...(e.parentElement?e.parentElement.children:[])].filter(x=>x.tagName===e.tagName);if(same.length>1)part+=':nth-of-type('+(same.indexOf(e)+1)+')';parts.unshift(part);e=e.parentElement;if(parts.length>=8)break;}return 'body>'+parts.join('>');};
const all=[...document.querySelectorAll('button,a,input,textarea,select,summary,[role],[tabindex],h1,h2,h3,h4,h5,h6,label')].filter(visible).slice(0,MAXE);
const elements=all.map((e,i)=>{const r=e.getBoundingClientRect();const meta=((e.id||'')+' '+(e.getAttribute('name')||'')+' '+(e.getAttribute('autocomplete')||'')+' '+(e.getAttribute('type')||'')).toLowerCase();const sensitive=e.tagName==='INPUT'&&(e.type==='password'||/(password|passwd|secret|token|api.?key|authorization)/.test(meta));return {ref:'B'+(i+1),selector:css(e),tag:e.tagName.toLowerCase(),inputType:(e.tagName==='INPUT'?(e.type||'text').toLowerCase():null),role:role(e),name:name(e).slice(0,180),text:(e.innerText||e.textContent||'').trim().slice(0,220),value:('value'in e?(sensitive?'[REDACTED]':String(e.value).slice(0,220)):null),checked:(e.tagName==='INPUT'&&['checkbox','radio'].includes((e.type||'').toLowerCase())?!!e.checked:null),disabled:!!e.disabled,focused:e===document.activeElement,rect:{x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}};});
const textBlocks=[...document.querySelectorAll('p,li,td,th,pre,code')].filter(visible).map(e=>(e.innerText||e.textContent||'').trim()).filter(Boolean).slice(0,MAXT).map(x=>x.slice(0,260));
const d=document.documentElement,b=document.body;const totalWidth=Math.max(d.scrollWidth,d.offsetWidth,d.clientWidth,b?b.scrollWidth:0,b?b.offsetWidth:0);const totalHeight=Math.max(d.scrollHeight,d.offsetHeight,d.clientHeight,b?b.scrollHeight:0,b?b.offsetHeight:0);
return {page:{url:location.href,title:document.title,scrollX:window.scrollX,scrollY:window.scrollY,viewportWidth:window.innerWidth,viewportHeight:window.innerHeight,totalWidth,totalHeight,atTop:window.scrollY<=1,atBottom:(window.scrollY+window.innerHeight)>=totalHeight-2},elements,textBlocks};
})()"""
        script = script.replace("MAXE", str(max_elements)).replace("MAXT", str(max_text_blocks))
        value = self._evaluate(session_id, script, timeout=10.0)
        if not isinstance(value, dict):
            raise BrowserError("BROWSER_SNAPSHOT_FAILED")
        elements = value.get("elements") if isinstance(value.get("elements"), list) else []
        session = self._session(session_id)
        session.refs = {}
        compact = []
        for item in elements:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            ref = str(item.get("ref") or "")
            selector = str(item.pop("selector", "") or "")
            if ref and selector:
                session.refs[ref] = selector
            compact.append({
                key: self._redact_snapshot_text(val)
                for key, val in item.items()
            })
        text_blocks = [
            self._redact_snapshot_text(item)
            for item in (value.get("textBlocks") or [])
            if isinstance(item, str)
        ]
        return {
            "schema": "easychange.browser-snapshot/1",
            "session_id": session_id,
            "page": value.get("page") if isinstance(value.get("page"), dict) else {},
            "elements": compact,
            "text": text_blocks,
            "ref_count": len(session.refs),
            "refs_valid_until": "next_snapshot_or_navigation",
            "window": self._window_info(session_id),
        }

    def _resolve_locator(self, session_id: str, locator: Any) -> Any:
        if isinstance(locator, str) and locator.startswith("B") and locator[1:].isdigit():
            selector = self._session(session_id).refs.get(locator)
            if not selector:
                raise LookupError(f"BROWSER_REF_NOT_FOUND_OR_STALE:{locator}")
            return {"selector": selector}
        return locator

    def wait_ready(self, session_id: str, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + max(0.1, float(timeout))
        last = None
        while time.monotonic() < deadline:
            last = self._evaluate(
                session_id,
                "({readyState:document.readyState,url:location.href,title:document.title})",
                timeout=min(2.0, max(0.2, deadline - time.monotonic())),
            )
            if isinstance(last, dict):
                current_url = str(last.get("url") or "")
                if current_url.casefold().startswith("chrome-error://"):
                    raise BrowserError(
                        "BROWSER_NAVIGATION_ERROR_PAGE:" + current_url[:500]
                    )
                if last.get("readyState") in {"interactive", "complete"}:
                    return last
            time.sleep(0.04)
        raise TimeoutError(f"BROWSER_READY_TIMEOUT:{last}")

    def navigate(self, session_id: str, url: str, timeout: float = 20.0) -> dict:
        if not str(url or "").strip():
            raise ValueError("BROWSER_URL_REQUIRED")
        session = self._session(session_id)
        session.refs = {}
        result = session.cdp.call(
            "Page.navigate", {"url": str(url)}, timeout=min(10.0, max(2.0, timeout))
        )
        if result.get("errorText"):
            raise BrowserError(f"BROWSER_NAVIGATION_FAILED:{result['errorText']}")
        return self.wait_ready(session_id, timeout)

    @staticmethod
    def _locator(locator: Any) -> str:
        if isinstance(locator, str):
            locator = {"selector": locator}
        if not isinstance(locator, dict):
            raise ValueError("BROWSER_LOCATOR_MUST_BE_STRING_OR_OBJECT")
        allowed = {"selector", "text", "exact_text", "role", "name", "index"}
        return json.dumps(
            {str(k): v for k, v in locator.items() if str(k) in allowed},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _element_js(self, locator: Any, body: str) -> str:
        template = """(() => {
const l=__LOCATOR__;
const visible=(e)=>!!(e&&(e.offsetWidth||e.offsetHeight||e.getClientRects().length)&&getComputedStyle(e).visibility!=='hidden'&&getComputedStyle(e).display!=='none');
const role=(e)=>{if(e.getAttribute('role'))return e.getAttribute('role');if(e.tagName==='BUTTON')return 'button';if(e.tagName==='A')return 'link';if(e.tagName==='TEXTAREA')return 'textbox';if(e.tagName==='SELECT')return 'combobox';if(e.tagName==='INPUT'){const t=(e.type||'text').toLowerCase();if(t==='checkbox')return 'checkbox';if(t==='radio')return 'radio';if(['button','submit','reset'].includes(t))return 'button';if(t==='range')return 'slider';return 'textbox';}return e.tagName.toLowerCase();};
const name=(e)=>e.getAttribute('aria-label')||e.getAttribute('title')||e.getAttribute('placeholder')||(e.innerText||e.textContent||'').trim();
let nodes=l.selector?[...document.querySelectorAll(l.selector)]:[...document.querySelectorAll('body *')];
if(l.role)nodes=nodes.filter(e=>role(e).toLowerCase()===String(l.role).toLowerCase());
if(l.name)nodes=nodes.filter(e=>name(e).trim().toLowerCase()===String(l.name).trim().toLowerCase());
if(l.exact_text!==undefined)nodes=nodes.filter(e=>(e.innerText||e.textContent||'').trim()===String(l.exact_text));
if(l.text!==undefined)nodes=nodes.filter(e=>(e.innerText||e.textContent||'').includes(String(l.text)));
nodes=nodes.filter(visible);
const e=nodes[Math.max(0,Number(l.index||0))];
if(!e)return {found:false,count:nodes.length};
__BODY__
})()"""
        return template.replace("__LOCATOR__", self._locator(locator)).replace("__BODY__", body)

    def inspect(self, session_id: str, locator: Any) -> dict:
        locator = self._resolve_locator(session_id, locator)
        value = self._evaluate(
            session_id,
            self._element_js(
                locator,
                """e.scrollIntoView({block:'center',inline:'center'});const r=e.getBoundingClientRect();return {found:true,count:nodes.length,tag:e.tagName.toLowerCase(),text:(e.innerText||e.textContent||'').trim().slice(0,4000),value:('value' in e?String(e.value):null),disabled:!!e.disabled,rect:{x:r.x,y:r.y,width:r.width,height:r.height},name:name(e),role:role(e),href:e.href||null};""",
            ),
        )
        return value if isinstance(value, dict) else {"found": False}

    def wait(
        self,
        session_id: str,
        *,
        locator: Any = None,
        text: str = "",
        url_contains: str = "",
        timeout: float = 10.0,
    ) -> dict:
        deadline = time.monotonic() + max(0.05, float(timeout))
        last = None
        while time.monotonic() < deadline:
            if locator is not None:
                last = self.inspect(session_id, locator)
                if last.get("found"):
                    return {"matched": True, "kind": "locator", "element": last}
            elif text:
                last = self._evaluate(
                    session_id,
                    f"document.body?document.body.innerText.includes({json.dumps(text)}):false",
                )
                if last:
                    return {"matched": True, "kind": "text", "text": text}
            elif url_contains:
                last = self.url(session_id)
                if url_contains in last:
                    return {"matched": True, "kind": "url", "url": last}
            else:
                return self.wait_ready(session_id, timeout)
            time.sleep(0.05)
        raise TimeoutError(f"BROWSER_WAIT_TIMEOUT:{last}")

    def click(self, session_id: str, locator: Any, timeout: float = 10.0) -> dict:
        self.wait(session_id, locator=locator, timeout=timeout)
        element = self.inspect(session_id, locator)
        rect = element.get("rect") if isinstance(element.get("rect"), dict) else {}
        x = float(rect.get("x") or 0) + float(rect.get("width") or 0) / 2
        y = float(rect.get("y") or 0) + float(rect.get("height") or 0) / 2
        cdp = self._session(session_id).cdp
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, timeout)
        cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}, timeout)
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}, timeout)
        return {"clicked": True, "x": round(x, 2), "y": round(y, 2), "element": element}

    def fill(self, session_id: str, locator: Any, value: str, timeout: float = 10.0) -> dict:
        self.wait(session_id, locator=locator, timeout=timeout)
        locator = self._resolve_locator(session_id, locator)
        result = self._evaluate(
            session_id,
            self._element_js(
                locator,
                f"""e.focus();const v={json.dumps(str(value), ensure_ascii=False)};if(e.isContentEditable){{e.textContent=v;}}else{{const p=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;const d=Object.getOwnPropertyDescriptor(p,'value');if(d&&d.set)d.set.call(e,v);else e.value=v;}}e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));return {{found:true,value:('value'in e?String(e.value):(e.textContent||''))}};""",
            ),
        )
        if not isinstance(result, dict) or not result.get("found"):
            raise BrowserError("BROWSER_FILL_TARGET_NOT_FOUND")
        return {"filled": True, "value_length": len(str(value)), "current_value": result.get("value")}

    def type_text(self, session_id: str, locator: Any, text: str, timeout: float = 10.0) -> dict:
        self.wait(session_id, locator=locator, timeout=timeout)
        locator = self._resolve_locator(session_id, locator)
        self._evaluate(session_id, self._element_js(locator, "e.focus();return {found:true};"))
        self._session(session_id).cdp.call("Input.insertText", {"text": str(text)}, timeout)
        return {"typed": True, "chars": len(str(text))}

    def press(self, session_id: str, key: str, timeout: float = 5.0) -> dict:
        raw = str(key or "").strip()
        if not raw:
            raise ValueError("BROWSER_KEY_REQUIRED")
        parts = [part.strip() for part in raw.split("+") if part.strip()]
        modifiers = 0
        aliases = {
            "ALT": 1,
            "CTRL": 2,
            "CONTROL": 2,
            "META": 4,
            "CMD": 4,
            "COMMAND": 4,
            "WIN": 4,
            "SHIFT": 8,
        }
        key_part = parts[-1]
        for part in parts[:-1]:
            bit = aliases.get(part.upper())
            if bit is None:
                raise ValueError(f"UNSUPPORTED_KEY_MODIFIER:{part}")
            modifiers |= bit
        mapping = {
            "ENTER": ("Enter", "Enter", 13, "\r"),
            "TAB": ("Tab", "Tab", 9, ""),
            "ESCAPE": ("Escape", "Escape", 27, ""),
            "ESC": ("Escape", "Escape", 27, ""),
            "SPACE": (" ", "Space", 32, " "),
            "ARROWDOWN": ("ArrowDown", "ArrowDown", 40, ""),
            "ARROWUP": ("ArrowUp", "ArrowUp", 38, ""),
            "ARROWLEFT": ("ArrowLeft", "ArrowLeft", 37, ""),
            "ARROWRIGHT": ("ArrowRight", "ArrowRight", 39, ""),
            "HOME": ("Home", "Home", 36, ""),
            "END": ("End", "End", 35, ""),
            "PAGEUP": ("PageUp", "PageUp", 33, ""),
            "PAGEDOWN": ("PageDown", "PageDown", 34, ""),
            "BACKSPACE": ("Backspace", "Backspace", 8, ""),
            "DELETE": ("Delete", "Delete", 46, ""),
            "INSERT": ("Insert", "Insert", 45, ""),
        }
        upper = key_part.upper()
        if upper.startswith("F") and upper[1:].isdigit() and 1 <= int(upper[1:]) <= 12:
            number = int(upper[1:])
            key_value, code, vk, text_value = upper, upper, 111 + number, ""
        else:
            key_value, code, vk, text_value = mapping.get(
                upper,
                (
                    key_part,
                    ("Key" + key_part.upper()) if len(key_part) == 1 and key_part.isalpha()
                    else ("Digit" + key_part if len(key_part) == 1 and key_part.isdigit() else key_part),
                    ord(key_part.upper()[0]) if len(key_part) == 1 else 0,
                    key_part if len(key_part) == 1 and modifiers == 0 else "",
                ),
            )
        params = {
            "key": key_value,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": modifiers,
        }
        if text_value:
            params["text"] = text_value
        cdp = self._session(session_id).cdp
        cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", **params}, timeout)
        cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", **params}, timeout)
        return {
            "pressed": True,
            "key": key_value,
            "chord": raw,
            "modifiers": modifiers,
        }

    def hover(self, session_id: str, locator: Any, timeout: float = 10.0) -> dict:
        self.wait(session_id, locator=locator, timeout=timeout)
        element = self.inspect(session_id, locator)
        rect = element.get("rect") if isinstance(element.get("rect"), dict) else {}
        x = float(rect.get("x") or 0) + float(rect.get("width") or rect.get("w") or 0) / 2
        y = float(rect.get("y") or 0) + float(rect.get("height") or rect.get("h") or 0) / 2
        self._session(session_id).cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y},
            timeout=timeout,
        )
        return {"hovered": True, "x": round(x, 2), "y": round(y, 2), "element": element}

    def double_click(self, session_id: str, locator: Any, timeout: float = 10.0) -> dict:
        self.wait(session_id, locator=locator, timeout=timeout)
        element = self.inspect(session_id, locator)
        rect = element.get("rect") if isinstance(element.get("rect"), dict) else {}
        x = float(rect.get("x") or 0) + float(rect.get("width") or rect.get("w") or 0) / 2
        y = float(rect.get("y") or 0) + float(rect.get("height") or rect.get("h") or 0) / 2
        cdp = self._session(session_id).cdp
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, timeout)
        cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 2}, timeout)
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 2}, timeout)
        return {"double_clicked": True, "x": round(x, 2), "y": round(y, 2), "element": element}

    def mouse_move(self, session_id: str, x: float, y: float, timeout: float = 5.0) -> dict:
        self._session(session_id).cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": float(x), "y": float(y)},
            timeout=timeout,
        )
        return {"moved": True, "x": float(x), "y": float(y)}

    def mouse_click(
        self,
        session_id: str,
        x: float,
        y: float,
        *,
        button: str = "left",
        click_count: int = 1,
        timeout: float = 5.0,
    ) -> dict:
        button = str(button or "left").casefold()
        if button not in {"left", "middle", "right"}:
            raise ValueError("UNSUPPORTED_MOUSE_BUTTON")
        count = max(1, min(3, int(click_count)))
        cdp = self._session(session_id).cdp
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": float(x), "y": float(y)}, timeout)
        cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": float(x), "y": float(y), "button": button, "clickCount": count}, timeout)
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": float(x), "y": float(y), "button": button, "clickCount": count}, timeout)
        return {"clicked": True, "x": float(x), "y": float(y), "button": button, "click_count": count}

    def drag(
        self,
        session_id: str,
        *,
        source: Any = None,
        target: Any = None,
        from_x: float | None = None,
        from_y: float | None = None,
        to_x: float | None = None,
        to_y: float | None = None,
        steps: int = 8,
        timeout: float = 10.0,
    ) -> dict:
        def center(locator: Any) -> tuple[float, float]:
            element = self.inspect(session_id, locator)
            rect = element.get("rect") if isinstance(element.get("rect"), dict) else {}
            return (
                float(rect.get("x") or 0) + float(rect.get("width") or rect.get("w") or 0) / 2,
                float(rect.get("y") or 0) + float(rect.get("height") or rect.get("h") or 0) / 2,
            )
        sx, sy = center(source) if source is not None else (float(from_x or 0), float(from_y or 0))
        tx, ty = center(target) if target is not None else (float(to_x or 0), float(to_y or 0))
        cdp = self._session(session_id).cdp
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": sx, "y": sy}, timeout)
        cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": sx, "y": sy, "button": "left", "clickCount": 1}, timeout)
        count = max(1, min(30, int(steps)))
        for index in range(1, count + 1):
            ratio = index / count
            x = sx + (tx - sx) * ratio
            y = sy + (ty - sy) * ratio
            cdp.call(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": x, "y": y, "button": "left"},
                timeout,
            )
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": tx, "y": ty, "button": "left", "clickCount": 1}, timeout)
        return {"dragged": True, "from": {"x": sx, "y": sy}, "to": {"x": tx, "y": ty}, "steps": count}

    def wheel(self, session_id: str, delta_x: float = 0, delta_y: float = 0, timeout: float = 5.0) -> dict:
        pos = self.position(session_id)
        x = float(pos.get("viewportWidth") or 0) / 2
        y = float(pos.get("viewportHeight") or 0) / 2
        self._session(session_id).cdp.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": x,
                "y": y,
                "deltaX": float(delta_x),
                "deltaY": float(delta_y),
            },
            timeout=timeout,
        )
        time.sleep(0.03)
        return self.position(session_id)

    def scroll(
        self,
        session_id: str,
        *,
        x: float | None = None,
        y: float | None = None,
        dx: float = 0,
        dy: float = 0,
        locator: Any = None,
        behavior: str = "instant",
    ) -> dict:
        behavior = "smooth" if str(behavior).casefold() == "smooth" else "auto"
        if locator is not None:
            locator = self._resolve_locator(session_id, locator)
            self._evaluate(
                session_id,
                self._element_js(
                    locator,
                    "e.scrollIntoView({block:'center',inline:'center',behavior:'%s'});return {found:true};" % behavior,
                ),
            )
        elif x is not None or y is not None:
            current = self.position(session_id)
            target_x = float(current.get("scrollX") or 0) if x is None else float(x)
            target_y = float(current.get("scrollY") or 0) if y is None else float(y)
            self._evaluate(
                session_id,
                "window.scrollTo({left:%s,top:%s,behavior:'%s'});true" % (target_x, target_y, behavior),
            )
        else:
            self._evaluate(
                session_id,
                "window.scrollBy({left:%s,top:%s,behavior:'%s'});true" % (float(dx), float(dy), behavior),
            )
        time.sleep(0.05 if behavior == "auto" else 0.25)
        return self.position(session_id)

    def focus(self, session_id: str, locator: Any) -> dict:
        locator = self._resolve_locator(session_id, locator)
        result = self._evaluate(
            session_id,
            self._element_js(locator, "e.focus();return {found:true,tag:e.tagName.toLowerCase()};"),
        )
        if not isinstance(result, dict) or not result.get("found"):
            raise BrowserError("BROWSER_FOCUS_TARGET_NOT_FOUND")
        return {"focused": True, "element": result}

    def check(self, session_id: str, locator: Any, checked: bool = True) -> dict:
        locator = self._resolve_locator(session_id, locator)
        value = "true" if checked else "false"
        result = self._evaluate(
            session_id,
            self._element_js(
                locator,
                "if(!('checked' in e))throw new Error('not checkable');if(e.checked!==%s){e.click();}return {found:true,checked:!!e.checked};" % value,
            ),
        )
        if not isinstance(result, dict) or bool(result.get("checked")) != bool(checked):
            raise BrowserError("BROWSER_CHECK_STATE_NOT_APPLIED")
        return {"checked": bool(result.get("checked"))}

    def select(self, session_id: str, locator: Any, values: list[str] | str) -> dict:
        locator = self._resolve_locator(session_id, locator)
        requested = [str(v) for v in (values if isinstance(values, list) else [values])]
        encoded = json.dumps(requested, ensure_ascii=False)
        result = self._evaluate(
            session_id,
            self._element_js(
                locator,
                "if(e.tagName!=='SELECT')throw new Error('not select');const wanted=%s;for(const o of e.options)o.selected=wanted.includes(String(o.value))||wanted.includes(String(o.text));e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return {found:true,values:[...e.selectedOptions].map(o=>o.value),texts:[...e.selectedOptions].map(o=>o.text)};" % encoded,
            ),
        )
        if not isinstance(result, dict):
            raise BrowserError("BROWSER_SELECT_FAILED")
        return result

    def reload(self, session_id: str, *, ignore_cache: bool = False, timeout: float = 15.0) -> dict:
        self._session(session_id).cdp.call(
            "Page.reload",
            {"ignoreCache": bool(ignore_cache)},
            timeout=min(10.0, timeout),
        )
        return self.wait_ready(session_id, timeout)

    def history(self, session_id: str, direction: int, timeout: float = 15.0) -> dict:
        result = self._session(session_id).cdp.call("Page.getNavigationHistory", timeout=5.0)
        entries = result.get("entries") if isinstance(result.get("entries"), list) else []
        current = int(result.get("currentIndex") or 0)
        target = current + (1 if direction > 0 else -1)
        if target < 0 or target >= len(entries):
            return {"moved": False, "reason": "NO_HISTORY_ENTRY", **self.position(session_id)}
        entry_id = int(entries[target]["id"])
        self._session(session_id).cdp.call(
            "Page.navigateToHistoryEntry",
            {"entryId": entry_id},
            timeout=5.0,
        )
        ready = self.wait_ready(session_id, timeout)
        return {"moved": True, "direction": "forward" if direction > 0 else "back", **ready}

    def zoom(self, session_id: str, scale: float) -> dict:
        scale = max(0.25, min(5.0, float(scale)))
        self._session(session_id).cdp.call(
            "Emulation.setPageScaleFactor",
            {"pageScaleFactor": scale},
            timeout=5.0,
        )
        return {"scale": scale}

    def tabs(self, session_id: str = "default") -> dict:
        session = self._session(session_id)
        values = self._http_json(f"http://127.0.0.1:{session.port}/json/list")
        tabs = []
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, dict) or item.get("type") != "page":
                continue
            tabs.append({
                "target_id": str(item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "active": str(item.get("id") or "") == session.target_id,
            })
        return {"session_id": session_id, "count": len(tabs), "tabs": tabs}

    def _reconnect_current_target_socket(self, session_id: str):
        session = self._session(session_id)
        values = self._http_json(f"http://127.0.0.1:{session.port}/json/list")
        target = next(
            (
                item for item in values
                if isinstance(item, dict)
                and str(item.get("id") or "") == str(session.target_id)
                and item.get("webSocketDebuggerUrl")
            ),
            None,
        )
        if target is None:
            raise LookupError(f"BROWSER_TAB_NOT_FOUND:{session.target_id}")
        new_cdp = _WebSocket(str(target["webSocketDebuggerUrl"]), timeout=5.0)
        for domain in ("Page.enable", "Runtime.enable", "DOM.enable"):
            new_cdp.call(domain, timeout=3.0)
        old = session.cdp
        session.cdp = new_cdp
        session.websocket_url = str(target["webSocketDebuggerUrl"])
        old.close()
        return new_cdp

    def _connect_target(self, session_id: str, target_id: str) -> dict:
        session = self._session(session_id)
        values = self._http_json(f"http://127.0.0.1:{session.port}/json/list")
        target = next(
            (
                item for item in values
                if isinstance(item, dict)
                and str(item.get("id") or "") == str(target_id)
                and item.get("webSocketDebuggerUrl")
            ),
            None,
        )
        if target is None:
            raise LookupError(f"BROWSER_TAB_NOT_FOUND:{target_id}")
        session.browser_cdp.call("Target.activateTarget", {"targetId": str(target_id)}, timeout=5.0)
        new_cdp = _WebSocket(str(target["webSocketDebuggerUrl"]), timeout=5.0)
        for domain in ("Page.enable", "Runtime.enable", "DOM.enable"):
            new_cdp.call(domain, timeout=3.0)
        old = session.cdp
        session.cdp = new_cdp
        session.target_id = str(target_id)
        session.websocket_url = str(target["webSocketDebuggerUrl"])
        session.refs = {}
        old.close()
        return self.state(session_id)

    def new_tab(self, session_id: str, url: str = "about:blank", activate: bool = True) -> dict:
        session = self._session(session_id)
        created = session.browser_cdp.call("Target.createTarget", {"url": str(url)}, timeout=5.0)
        target_id = str(created.get("targetId") or "")
        if not target_id:
            raise BrowserError("BROWSER_NEW_TAB_FAILED")
        state = self._connect_target(session_id, target_id) if activate else self.tabs(session_id)
        return {"target_id": target_id, "activated": bool(activate), "state": state}

    def activate_tab(self, session_id: str, target_id: str) -> dict:
        return self._connect_target(session_id, target_id)

    def close_tab(self, session_id: str, target_id: str = "") -> dict:
        session = self._session(session_id)
        target = str(target_id or session.target_id)
        closing_current = target == session.target_id
        result = session.browser_cdp.call("Target.closeTarget", {"targetId": target}, timeout=5.0)
        if not bool(result.get("success", True)):
            raise BrowserError("BROWSER_CLOSE_TAB_FAILED")
        time.sleep(0.05)
        if closing_current:
            values = self.tabs(session_id).get("tabs") or []
            remaining = next((item for item in values if item.get("target_id") != target), None)
            if remaining:
                self._connect_target(session_id, str(remaining["target_id"]))
        return {"closed": True, "target_id": target, "tabs": self.tabs(session_id)}

    def screenshot(
        self,
        session_id: str,
        *,
        title: str = "browser",
        full_page: bool = True,
        path: Path | None = None,
    ) -> dict:
        session = self._session(session_id)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = Path(path) if path else self.evidence_root / f"{stamp}-{self._slug(title)}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        params: dict[str, Any] = {"format": "png", "fromSurface": True}
        if full_page:
            metrics = session.cdp.call("Page.getLayoutMetrics", timeout=5.0)
            size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            params["captureBeyondViewport"] = True
            params["clip"] = {
                "x": 0,
                "y": 0,
                "width": max(1, min(10000, int(float(size.get("width") or 1440)))),
                "height": max(1, min(30000, int(float(size.get("height") or 1000)))),
                "scale": 1,
            }
        result = session.cdp.call("Page.captureScreenshot", params, timeout=15.0)
        raw = base64.b64decode(str(result.get("data") or ""))
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise BrowserError("BROWSER_SCREENSHOT_INVALID_PNG")
        target.write_bytes(raw)
        return {
            "path": str(target),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "full_page": bool(full_page),
            "url": self.url(session_id),
        }

    def _cdp_channel(self, session_id: str, channel: str):
        session = self._session(session_id)
        normalized = str(channel or "page").casefold()
        if normalized in {"page", "target", "tab"}:
            return session.cdp, "page"
        if normalized in {"browser", "root"}:
            return session.browser_cdp, "browser"
        raise ValueError("CDP_CHANNEL_MUST_BE_PAGE_OR_BROWSER")

    @staticmethod
    def _result_ref(value: Any, context: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            if set(value) == {"$result_ref"}:
                path = str(value.get("$result_ref") or "")
                parts = [part for part in path.split(".") if part]
                if not parts:
                    raise ValueError("CDP_RESULT_REF_REQUIRED")
                current: Any = context
                for part in parts:
                    if isinstance(current, dict):
                        if part not in current:
                            raise KeyError(f"CDP_RESULT_REF_NOT_FOUND:{path}")
                        current = current[part]
                    elif isinstance(current, list) and part.isdigit():
                        index = int(part)
                        if index < 0 or index >= len(current):
                            raise IndexError(f"CDP_RESULT_REF_NOT_FOUND:{path}")
                        current = current[index]
                    else:
                        raise KeyError(f"CDP_RESULT_REF_NOT_FOUND:{path}")
                return current
            return {
                key: BrowserService._result_ref(item, context)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [BrowserService._result_ref(item, context) for item in value]
        return value

    def cdp_call(
        self,
        session_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        channel: str = "page",
        timeout: float = 10.0,
        include_events: bool = True,
    ) -> dict:
        method = str(method or "").strip()
        if not method or "." not in method:
            raise ValueError("CDP_METHOD_REQUIRED")
        if method in {"Browser.close", "DOM.setFileInputFiles"}:
            raise BrowserError(f"CDP_METHOD_FORBIDDEN_BY_SESSION_POLICY:{method}")
        socket_channel, normalized = self._cdp_channel(session_id, channel)
        result = socket_channel.call(
            method,
            dict(params or {}),
            timeout=max(0.1, min(120.0, float(timeout))),
        )
        events = socket_channel.drain_events(128) if include_events else []
        return {
            "schema": "easychange.browser-cdp/1",
            "session_id": self._safe_id(session_id),
            "channel": normalized,
            "method": method,
            "result": self._redact_value(result),
            "events": self._redact_value(events),
            "event_count": len(events),
            "secrets_exposed": False,
        }

    def cdp_batch(
        self,
        session_id: str,
        calls: list[dict],
        *,
        continue_on_error: bool = False,
        timeout: float = 10.0,
    ) -> dict:
        if not isinstance(calls, list) or not 1 <= len(calls) <= 64:
            raise ValueError("CDP_BATCH_REQUIRES_1_TO_64_CALLS")
        context: dict[str, Any] = {}
        results: list[dict] = []
        failed = 0
        started = time.monotonic()
        for index, raw in enumerate(calls):
            if not isinstance(raw, dict):
                raise ValueError(f"CDP_CALL_OBJECT_REQUIRED:{index}")
            method = str(raw.get("method") or "").strip()
            channel = str(raw.get("channel") or "page")
            params = self._result_ref(
                raw.get("params") if isinstance(raw.get("params"), dict) else {},
                context,
            )
            label = str(raw.get("save_as") or f"r{index}")
            try:
                socket_channel, normalized = self._cdp_channel(session_id, channel)
                if method in {"Browser.close", "DOM.setFileInputFiles"}:
                    raise BrowserError(f"CDP_METHOD_FORBIDDEN_BY_SESSION_POLICY:{method}")
                raw_result = socket_channel.call(
                    method,
                    params,
                    timeout=max(
                        0.1,
                        min(120.0, float(raw.get("timeout") or timeout)),
                    ),
                )
                context[label] = raw_result
                events = socket_channel.drain_events(
                    max(0, min(256, int(raw.get("event_limit") or 64)))
                )
                results.append({
                    "index": index,
                    "ok": True,
                    "method": method,
                    "channel": normalized,
                    "save_as": label,
                    "result": self._redact_value(raw_result),
                    "events": self._redact_value(events),
                })
            except Exception as exc:
                failed += 1
                results.append({
                    "index": index,
                    "ok": False,
                    "method": method,
                    "channel": channel,
                    "save_as": label,
                    "error": f"{type(exc).__name__}:{str(exc)[:1000]}",
                })
                if not continue_on_error:
                    break
        return {
            "schema": "easychange.browser-cdp-batch/1",
            "session_id": self._safe_id(session_id),
            "ok": failed == 0,
            "count": len(results),
            "failed": failed,
            "results": results,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "result_refs_supported": True,
            "secrets_exposed": False,
        }

    def cdp_events(
        self,
        session_id: str,
        *,
        channel: str = "page",
        limit: int = 100,
        pump: bool = True,
    ) -> dict:
        socket_channel, normalized = self._cdp_channel(session_id, channel)
        if pump:
            if normalized == "page":
                socket_channel.call(
                    "Runtime.evaluate",
                    {"expression": "0", "returnByValue": True},
                    timeout=2.0,
                )
            else:
                socket_channel.call("Browser.getVersion", timeout=2.0)
        events = socket_channel.drain_events(max(0, min(512, int(limit or 0))))
        event_status = (
            socket_channel.event_status()
            if hasattr(socket_channel, "event_status")
            else {
                "reader_alive": False,
                "pending_events": None,
                "events_dropped": 0,
                "reader_error": None,
            }
        )
        return {
            "schema": "easychange.browser-cdp-events/1",
            "session_id": self._safe_id(session_id),
            "channel": normalized,
            "count": len(events),
            "events": self._redact_value(events),
            "event_reader": event_status,
            "continuous_reader": bool(event_status.get("reader_alive")),
            "secrets_exposed": False,
        }

    @staticmethod
    def _redact_value(value: Any) -> Any:
        if isinstance(value, str):
            return BrowserService._redact_snapshot_text(value)
        if isinstance(value, list):
            return [BrowserService._redact_value(item) for item in value]
        if isinstance(value, dict):
            output = {}
            for key, item in value.items():
                normalized = str(key).casefold()
                if any(token in normalized for token in (
                    "password", "passwd", "secret", "token",
                    "api_key", "apikey", "authorization", "cookie",
                )):
                    output[key] = "[REDACTED]" if item not in (None, "", [], {}) else item
                else:
                    output[key] = BrowserService._redact_value(item)
            return output
        return value

    def evaluate(self, session_id: str, script: str, timeout: float = 10.0) -> dict:
        payload = str(script or "")
        size = len(payload.encode("utf-8"))
        if not size:
            raise ValueError("BROWSER_SCRIPT_REQUIRED")
        if size > 64 * 1024:
            raise ValueError("BROWSER_SCRIPT_TOO_LARGE")
        return {
            "value": self._redact_value(self._evaluate(session_id, payload, timeout)),
            "script_bytes": size,
            "temporary": True,
            "persisted": False,
        }

    def dom(
        self,
        session_id: str = "default",
        *,
        cursor: int = 0,
        max_bytes: int = 65536,
    ) -> dict:
        cursor = max(0, int(cursor or 0))
        max_bytes = max(1024, min(1024 * 1024, int(max_bytes or 65536)))
        script = """(() => {
const root=document.documentElement.cloneNode(true);
const sensitive=(e)=>{
 const meta=((e.id||'')+' '+(e.getAttribute('name')||'')+' '+(e.getAttribute('autocomplete')||'')+' '+(e.getAttribute('type')||'')).toLowerCase();
 return e.tagName==='INPUT'&&((e.type||'').toLowerCase()==='password'||/(password|passwd|pwd|secret|token|api.?key|authorization|cookie)/.test(meta));
};
for(const e of root.querySelectorAll('input,textarea')){
 if(sensitive(e)){e.setAttribute('value','[REDACTED]');e.textContent='[REDACTED]';}
}
return {html:'<!doctype html>'+String.fromCharCode(10)+root.outerHTML,url:location.href,title:document.title,
 scrollX:window.scrollX,scrollY:window.scrollY,viewportWidth:window.innerWidth,
 viewportHeight:window.innerHeight,totalWidth:Math.max(document.documentElement.scrollWidth,document.body?document.body.scrollWidth:0),
 totalHeight:Math.max(document.documentElement.scrollHeight,document.body?document.body.scrollHeight:0),
 focused:(document.activeElement&&document.activeElement.tagName)||null};
})()"""
        value = self._evaluate(session_id, script, timeout=20.0)
        if not isinstance(value, dict):
            raise BrowserError("BROWSER_DOM_FAILED")
        html_text = self._redact_snapshot_text(str(value.pop("html", "") or ""))
        raw = html_text.encode("utf-8")
        total = len(raw)
        start = min(cursor, total)
        end = min(total, start + max_bytes)
        while end > start:
            try:
                page = raw[start:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            page = ""
        next_cursor = end if end < total else None
        return {
            "schema": "easychange.browser-dom/1",
            "session_id": self._safe_id(session_id),
            "page": self._redact_value(value),
            "cursor": start,
            "next_cursor": next_cursor,
            "returned_bytes": end - start,
            "total_bytes": total,
            "remaining_bytes": max(0, total - end),
            "complete": next_cursor is None,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "text": page,
        }

    def visual(
        self,
        session_id: str = "default",
        *,
        quality: int = 55,
        full_page: bool = False,
        max_bytes: int = 262144,
    ) -> dict:
        quality = max(20, min(90, int(quality or 55)))
        max_bytes = max(32768, min(2 * 1024 * 1024, int(max_bytes or 262144)))
        session = self._session(session_id)
        attempts = []
        raw = b""
        current_quality = quality
        while current_quality >= 20:
            params: dict[str, Any] = {
                "format": "jpeg",
                "quality": current_quality,
                "fromSurface": True,
                "captureBeyondViewport": bool(full_page),
            }
            if full_page:
                metrics = session.cdp.call("Page.getLayoutMetrics", timeout=5.0)
                size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
                params["clip"] = {
                    "x": 0,
                    "y": 0,
                    "width": max(1, min(10000, int(float(size.get("width") or 1440)))),
                    "height": max(1, min(30000, int(float(size.get("height") or 1000)))),
                    "scale": 1,
                }
            try:
                result = session.cdp.call("Page.captureScreenshot", params, timeout=30.0)
            except TimeoutError:
                attempts.append({
                    "quality": current_quality,
                    "error": "TimeoutError",
                    "recovery": "RECONNECT_CURRENT_TARGET_ONCE",
                })
                session.cdp = self._reconnect_current_target_socket(session_id)
                result = session.cdp.call("Page.captureScreenshot", params, timeout=30.0)
            raw = base64.b64decode(str(result.get("data") or ""))
            attempts.append({"quality": current_quality, "bytes": len(raw)})
            if raw.startswith(b"\xff\xd8") and len(raw) <= max_bytes:
                break
            current_quality -= 10
        if not raw.startswith(b"\xff\xd8"):
            raise BrowserError("BROWSER_VISUAL_INVALID_JPEG")
        if len(raw) > max_bytes:
            raise BrowserError(f"BROWSER_VISUAL_TOO_LARGE:{len(raw)}>{max_bytes}")
        position = None
        position_error = None
        try:
            position = self.position(session_id)
        except Exception as exc:
            # Position is metadata only. A valid CDP screenshot must remain
            # usable even if Browser.getWindow* is temporarily slow/unavailable.
            position_error = f"{type(exc).__name__}:{str(exc)[:240]}"

        value = {
            "schema": "easychange.browser-visual/1",
            "session_id": self._safe_id(session_id),
            "mime": "image/jpeg",
            "encoding": "base64",
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "data_b64": base64.b64encode(raw).decode("ascii"),
            "quality": current_quality,
            "full_page": bool(full_page),
            "attempts": attempts,
            "position": position,
            "qr_transport": "EC2_RESULT_STORE_CHUNKS",
        }
        if position_error:
            value["position_error"] = position_error
        return value

    def swagger_operations(self, session_id: str = "default", *, limit: int = 500) -> dict:
        limit = max(1, min(2000, int(limit or 500)))
        script = """(() => {
const rows=[...document.querySelectorAll('.opblock')].slice(0,LIMIT).map((b,i)=>({
 index:i,
 method:(b.querySelector('.opblock-summary-method')?.textContent||'').trim().toUpperCase(),
 path:(b.querySelector('.opblock-summary-path')?.textContent||'').trim(),
 summary:(b.querySelector('.opblock-summary-description')?.textContent||'').trim(),
 expanded:b.classList.contains('is-open')
})).filter(x=>x.method&&x.path);
return {swaggerUi:!!document.querySelector('.swagger-ui'),url:location.href,title:document.title,operations:rows};
})()""".replace("LIMIT", str(limit))
        value = self._evaluate(session_id, script, timeout=10.0)
        if not isinstance(value, dict):
            raise BrowserError("SWAGGER_UI_NOT_AVAILABLE")
        return {
            "schema": "easychange.swagger-operations/1",
            "session_id": self._safe_id(session_id),
            "swagger_ui": bool(value.get("swaggerUi")),
            "url": str(value.get("url") or ""),
            "title": str(value.get("title") or ""),
            "operations": self._redact_value(value.get("operations") or []),
            "auth_values_exposed": False,
        }

    def swagger_execute(
        self,
        session_id: str,
        *,
        method: str,
        path: str,
        parameters: dict[str, Any] | None = None,
        body: Any = None,
        timeout: float = 30.0,
    ) -> dict:
        method = str(method or "").strip().upper()
        path = str(path or "").strip()
        if not method or not path:
            raise ValueError("SWAGGER_METHOD_AND_PATH_REQUIRED")
        parameters = dict(parameters or {})
        sensitive_input = any(
            any(token in str(key).casefold() for token in (
                "password", "passwd", "secret", "token",
                "api_key", "apikey", "authorization", "cookie",
            ))
            for key in parameters
        )
        config = json.dumps(
            {"method": method, "path": path, "parameters": parameters, "body": body},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        script = """(async()=> {
const cfg=CONFIG;
const blocks=[...document.querySelectorAll('.opblock')];
const b=blocks.find(x=>(x.querySelector('.opblock-summary-method')?.textContent||'').trim().toUpperCase()===cfg.method
 &&(x.querySelector('.opblock-summary-path')?.textContent||'').trim()===cfg.path);
if(!b)return {ok:false,error:'SWAGGER_OPERATION_NOT_FOUND'};
if(!b.classList.contains('is-open')){(b.querySelector('.opblock-summary-control')||b.querySelector('.opblock-summary'))?.click();await new Promise(r=>setTimeout(r,60));}
const t=[...b.querySelectorAll('button')].find(x=>/try it out/i.test(x.textContent||''));if(t){t.click();await new Promise(r=>setTimeout(r,60));}
const setv=(e,v)=>{const s=typeof v==='string'?v:JSON.stringify(v);e.focus();const p=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;const d=Object.getOwnPropertyDescriptor(p,'value');if(d&&d.set)d.set.call(e,s);else e.value=s;e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));};
for(const [n,v] of Object.entries(cfg.parameters||{})){const row=[...b.querySelectorAll('tr')].find(r=>(r.querySelector('.parameter__name')?.textContent||'').trim().split(/\s+/)[0]===n);const e=row?.querySelector('input,textarea,select');if(!e)return {ok:false,error:'SWAGGER_PARAMETER_NOT_FOUND:'+n};setv(e,v);}
if(cfg.body!==null&&cfg.body!==undefined){const e=b.querySelector('textarea.body-param__text,textarea');if(!e)return {ok:false,error:'SWAGGER_BODY_EDITOR_NOT_FOUND'};setv(e,cfg.body);}
const x=[...b.querySelectorAll('button')].find(x=>x.classList.contains('execute')||/^\s*execute\s*$/i.test(x.textContent||''));if(!x)return {ok:false,error:'SWAGGER_EXECUTE_BUTTON_NOT_FOUND'};x.click();return {ok:true};
})()""".replace("CONFIG", config)

        session = self._session(session_id)
        cdp = session.cdp
        try:
            cdp.call("Network.enable", {}, timeout=min(5.0, max(0.5, float(timeout))))
        except Exception as exc:
            raise BrowserError(
                "SWAGGER_NETWORK_OBSERVATION_FAILED:"
                + f"{type(exc).__name__}:{str(exc)[:300]}"
            ) from exc

        # Remove events from previous Swagger executions. Only a requestId
        # observed after this point can satisfy this call.
        cdp.drain_events(limit=512)

        started = self._evaluate(session_id, script, timeout=min(15.0, timeout))
        if not isinstance(started, dict) or not started.get("ok"):
            raise BrowserError(
                str(
                    (started or {}).get("error")
                    if isinstance(started, dict)
                    else "SWAGGER_EXECUTE_FAILED"
                )
            )

        template_parts = [
            urllib.parse.unquote(part)
            for part in urllib.parse.urlsplit(path).path.strip("/").split("/")
            if part
        ]

        def matches_request_url(url: str) -> bool:
            try:
                actual_parts = [
                    urllib.parse.unquote(part)
                    for part in urllib.parse.urlsplit(str(url or "")).path.strip("/").split("/")
                    if part
                ]
            except Exception:
                return False
            if len(actual_parts) != len(template_parts):
                return False
            for expected, actual in zip(template_parts, actual_parts):
                if expected.startswith("{") and expected.endswith("}"):
                    if not actual:
                        return False
                    continue
                if expected != actual:
                    return False
            return True

        deadline = time.monotonic() + max(1.0, min(120.0, float(timeout)))
        request_id = ""
        response_meta: dict[str, Any] | None = None
        response: dict[str, Any] | None = None
        request_failure = ""

        while time.monotonic() < deadline:
            for event in cdp.drain_events(limit=512):
                if not isinstance(event, dict):
                    continue
                event_method = str(event.get("method") or "")
                params = event.get("params")
                params = params if isinstance(params, dict) else {}

                if event_method == "Network.requestWillBeSent":
                    request = params.get("request")
                    request = request if isinstance(request, dict) else {}
                    if (
                        str(request.get("method") or "").upper() == method
                        and matches_request_url(str(request.get("url") or ""))
                    ):
                        request_id = str(params.get("requestId") or "")
                        response_meta = None
                        request_failure = ""
                    continue

                if not request_id or str(params.get("requestId") or "") != request_id:
                    continue

                if event_method == "Network.responseReceived":
                    current = params.get("response")
                    response_meta = current if isinstance(current, dict) else {}
                    continue

                if event_method == "Network.loadingFailed":
                    request_failure = str(params.get("errorText") or "REQUEST_FAILED")
                    break

                if event_method != "Network.loadingFinished" or response_meta is None:
                    continue

                body_text = ""
                try:
                    payload = cdp.call(
                        "Network.getResponseBody",
                        {"requestId": request_id},
                        timeout=min(10.0, max(0.5, float(timeout))),
                    )
                    raw_body = payload.get("body") if isinstance(payload, dict) else ""
                    encoded = (
                        bool(payload.get("base64Encoded"))
                        if isinstance(payload, dict)
                        else False
                    )
                    if encoded:
                        body_text = base64.b64decode(str(raw_body or "")).decode(
                            "utf-8", errors="replace"
                        )
                    else:
                        body_text = str(raw_body or "")
                except Exception:
                    body_text = ""

                status_value = response_meta.get("status")
                try:
                    status_number = float(status_value)
                    status_text = (
                        str(int(status_number))
                        if status_number.is_integer()
                        else str(status_number)
                    )
                except (TypeError, ValueError):
                    status_text = str(status_value or "")

                headers_value = response_meta.get("headers")
                if isinstance(headers_value, dict):
                    header_lines = [
                        f"{key}: {value}"
                        for key, value in sorted(
                            headers_value.items(),
                            key=lambda item: str(item[0]).casefold(),
                        )
                    ]
                    headers_text = "\n".join(header_lines)
                else:
                    headers_text = ""

                response = {
                    "status": status_text,
                    "body": body_text[:65536],
                    "headers": headers_text[:8192],
                }
                break

            if request_failure:
                raise BrowserError(
                    "SWAGGER_REQUEST_FAILED:" + request_failure[:500]
                )
            if response is not None:
                break
            time.sleep(0.04)

        if response is None:
            raise TimeoutError("SWAGGER_RESPONSE_TIMEOUT")

        return {
            "schema": "easychange.swagger-execution/1",
            "session_id": self._safe_id(session_id),
            "method": method,
            "path": path,
            "response": self._redact_value(response),
            "response_correlation": "CDP_NETWORK_REQUEST_ID",
            "existing_browser_auth_preserved": True,
            "sensitive_input_used": sensitive_input,
            "auth_values_exposed": False,
            "request_secrets_logged": False,
        }
    def _slug(value: str) -> str:
        text = "".join(ch if ch.isalnum() or ch in "_-" else "-" for ch in str(value or "browser"))
        return text[:80].strip("-") or "browser"

    def assert_condition(self, session_id: str, action: dict) -> dict:
        kind = str(action.get("assert") or action.get("condition") or "").casefold()
        if kind in {"visible", "exists"}:
            actual = self.inspect(session_id, action.get("locator"))
            expected, passed = True, bool(actual.get("found"))
        elif kind in {"text_contains", "text"}:
            expected = str(action.get("value") or action.get("text") or "")
            actual = (
                str(self.inspect(session_id, action.get("locator")).get("text") or "")
                if action.get("locator") is not None
                else str(self._evaluate(session_id, "document.body?document.body.innerText:''") or "")
            )
            passed = expected in actual
        elif kind == "value_equals":
            expected = str(action.get("value") or "")
            actual = str(self.inspect(session_id, action.get("locator")).get("value") or "")
            passed = actual == expected
        elif kind == "url_contains":
            expected, actual = str(action.get("value") or ""), self.url(session_id)
            passed = expected in actual
        elif kind == "title_contains":
            expected, actual = str(action.get("value") or ""), self.title(session_id)
            passed = expected in actual
        else:
            raise ValueError(f"UNSUPPORTED_BROWSER_ASSERTION:{kind}")
        if not passed:
            raise AssertionError(
                f"BROWSER_ASSERTION_FAILED:{kind}:expected={str(expected)[:300]}:actual={str(actual)[:500]}"
            )
        return {"assertion": kind, "passed": True, "expected": expected, "actual": actual}

    def execute_actions(
        self,
        session_id: str,
        actions: list[dict],
        *,
        continue_on_error: bool = False,
        default_timeout: float = 10.0,
        _depth: int = 0,
    ) -> dict:
        if not isinstance(actions, list) or len(actions) > 128:
            raise ValueError("BROWSER_ACTIONS_MUST_BE_ARRAY_MAX_128")
        if _depth > 3:
            raise ValueError("BROWSER_ACTION_NESTING_LIMIT")
        output, failed = [], 0
        started = time.monotonic()
        for index, raw in enumerate(actions):
            if not isinstance(raw, dict):
                raise ValueError(f"BROWSER_ACTION_OBJECT_REQUIRED:{index}")
            action = dict(raw)
            kind = str(action.get("type") or action.get("action") or "").casefold()
            timeout = max(0.1, min(120.0, float(action.get("timeout") or default_timeout)))
            step = time.monotonic()
            try:
                if kind == "navigate":
                    data = self.navigate(session_id, str(action.get("url") or ""), timeout)
                elif kind == "wait":
                    data = self.wait(
                        session_id,
                        locator=action.get("locator"),
                        text=str(action.get("text") or ""),
                        url_contains=str(action.get("url_contains") or ""),
                        timeout=timeout,
                    )
                elif kind == "click":
                    data = self.click(session_id, action.get("locator"), timeout)
                elif kind in {"double_click", "dblclick"}:
                    data = self.double_click(session_id, action.get("locator"), timeout)
                elif kind == "hover":
                    data = self.hover(session_id, action.get("locator"), timeout)
                elif kind == "fill":
                    data = self.fill(
                        session_id,
                        action.get("locator"),
                        str(action.get("value") or ""),
                        timeout,
                    )
                elif kind in {"type", "type_text"}:
                    data = self.type_text(
                        session_id,
                        action.get("locator"),
                        str(action.get("text") or ""),
                        timeout,
                    )
                elif kind == "press":
                    data = self.press(session_id, str(action.get("key") or ""), timeout)
                elif kind == "focus":
                    data = self.focus(session_id, action.get("locator"))
                elif kind == "check":
                    data = self.check(session_id, action.get("locator"), True)
                elif kind == "uncheck":
                    data = self.check(session_id, action.get("locator"), False)
                elif kind == "select":
                    data = self.select(
                        session_id,
                        action.get("locator"),
                        action.get("values") if action.get("values") is not None else action.get("value"),
                    )
                elif kind == "scroll":
                    data = self.scroll(
                        session_id,
                        x=action.get("x"),
                        y=action.get("y"),
                        dx=float(action.get("dx") or 0),
                        dy=float(action.get("dy") or 0),
                        locator=action.get("locator"),
                        behavior=str(action.get("behavior") or "instant"),
                    )
                elif kind == "drag":
                    data = self.drag(
                        session_id,
                        source=action.get("source"),
                        target=action.get("target"),
                        from_x=action.get("from_x"),
                        from_y=action.get("from_y"),
                        to_x=action.get("to_x"),
                        to_y=action.get("to_y"),
                        steps=int(action.get("steps") or 8),
                        timeout=timeout,
                    )
                elif kind == "wheel":
                    data = self.wheel(
                        session_id,
                        float(action.get("delta_x") or 0),
                        float(action.get("delta_y") or 0),
                        timeout,
                    )
                elif kind == "position":
                    data = self.position(session_id)
                elif kind == "snapshot":
                    data = self.snapshot(
                        session_id,
                        max_elements=int(action.get("max_elements") or 80),
                        max_text_blocks=int(action.get("max_text_blocks") or 40),
                    )
                elif kind == "dom":
                    data = self.dom(
                        session_id,
                        cursor=int(action.get("cursor") or 0),
                        max_bytes=int(action.get("max_bytes") or 65536),
                    )
                elif kind == "visual":
                    data = self.visual(
                        session_id,
                        quality=int(action.get("quality") or 55),
                        full_page=bool(action.get("full_page", False)),
                        max_bytes=int(action.get("max_bytes") or 262144),
                    )
                elif kind == "swagger_list":
                    data = self.swagger_operations(
                        session_id,
                        limit=int(action.get("limit") or 500),
                    )
                elif kind == "swagger_execute":
                    data = self.swagger_execute(
                        session_id,
                        method=str(action.get("method") or ""),
                        path=str(action.get("path") or ""),
                        parameters=action.get("parameters") if isinstance(action.get("parameters"), dict) else {},
                        body=action.get("body"),
                        timeout=timeout,
                    )
                elif kind == "cdp":
                    data = self.cdp_call(
                        session_id,
                        str(action.get("method") or ""),
                        action.get("params") if isinstance(action.get("params"), dict) else {},
                        channel=str(action.get("channel") or "page"),
                        timeout=timeout,
                        include_events=bool(action.get("include_events", True)),
                    )
                elif kind == "cdp_batch":
                    data = self.cdp_batch(
                        session_id,
                        action.get("calls") if isinstance(action.get("calls"), list) else [],
                        continue_on_error=bool(action.get("continue_on_error", False)),
                        timeout=timeout,
                    )
                elif kind == "cdp_events":
                    data = self.cdp_events(
                        session_id,
                        channel=str(action.get("channel") or "page"),
                        limit=int(action.get("limit") or 100),
                        pump=bool(action.get("pump", True)),
                    )
                elif kind == "show":
                    data = self.show(session_id)
                elif kind == "hide":
                    data = self.hide(session_id)
                elif kind == "mouse_move":
                    data = self.mouse_move(
                        session_id,
                        float(action.get("x") or 0),
                        float(action.get("y") or 0),
                        timeout,
                    )
                elif kind == "mouse_click":
                    data = self.mouse_click(
                        session_id,
                        float(action.get("x") or 0),
                        float(action.get("y") or 0),
                        button=str(action.get("button") or "left"),
                        click_count=int(action.get("click_count") or 1),
                        timeout=timeout,
                    )
                elif kind == "reload":
                    data = self.reload(
                        session_id,
                        ignore_cache=bool(action.get("ignore_cache", False)),
                        timeout=timeout,
                    )
                elif kind == "back":
                    data = self.history(session_id, -1, timeout)
                elif kind == "forward":
                    data = self.history(session_id, 1, timeout)
                elif kind == "zoom":
                    data = self.zoom(session_id, float(action.get("scale") or 1.0))
                elif kind == "tabs":
                    data = self.tabs(session_id)
                elif kind == "new_tab":
                    data = self.new_tab(
                        session_id,
                        str(action.get("url") or "about:blank"),
                        bool(action.get("activate", True)),
                    )
                elif kind == "activate_tab":
                    data = self.activate_tab(session_id, str(action.get("target_id") or ""))
                elif kind == "close_tab":
                    data = self.close_tab(session_id, str(action.get("target_id") or ""))
                elif kind == "assert":
                    data = self.assert_condition(session_id, action)
                elif kind == "inspect":
                    data = self.inspect(session_id, action.get("locator"))
                    if not data.get("found"):
                        raise BrowserError("BROWSER_ELEMENT_NOT_FOUND")
                elif kind == "screenshot":
                    data = self.screenshot(
                        session_id,
                        title=str(action.get("title") or f"step-{index}"),
                        full_page=bool(action.get("full_page", True)),
                    )
                elif kind == "evaluate":
                    data = self.evaluate(
                        session_id,
                        str(action.get("script") or ""),
                        timeout,
                    )
                elif kind == "sleep":
                    seconds = max(0.0, min(10.0, float(action.get("seconds") or 0)))
                    time.sleep(seconds)
                    data = {"slept_seconds": seconds}
                elif kind == "repeat":
                    nested = action.get("actions")
                    if not isinstance(nested, list) or len(nested) > 32:
                        raise ValueError("BROWSER_REPEAT_ACTIONS_MAX_32")
                    times = max(0, min(50, int(action.get("times") or 1)))
                    batches = []
                    for iteration in range(times):
                        batch = self.execute_actions(
                            session_id,
                            nested,
                            continue_on_error=bool(action.get("continue_on_error", False)),
                            default_timeout=timeout,
                            _depth=_depth + 1,
                        )
                        batches.append({"iteration": iteration + 1, "result": batch})
                        if not batch.get("ok") and not bool(action.get("continue_on_error", False)):
                            raise BrowserError(f"BROWSER_REPEAT_FAILED:{iteration + 1}")
                    data = {"iterations": times, "batches": batches}
                elif kind == "until":
                    nested = action.get("actions")
                    condition = action.get("condition")
                    if not isinstance(nested, list) or len(nested) > 32:
                        raise ValueError("BROWSER_UNTIL_ACTIONS_MAX_32")
                    if not isinstance(condition, dict):
                        raise ValueError("BROWSER_UNTIL_CONDITION_REQUIRED")
                    max_iterations = max(1, min(50, int(action.get("max_iterations") or 10)))
                    batches = []
                    matched = False
                    assertion = None
                    for iteration in range(max_iterations):
                        try:
                            assertion = self.assert_condition(session_id, condition)
                            matched = True
                            break
                        except Exception:
                            pass
                        batch = self.execute_actions(
                            session_id,
                            nested,
                            continue_on_error=False,
                            default_timeout=timeout,
                            _depth=_depth + 1,
                        )
                        batches.append({"iteration": iteration + 1, "result": batch})
                        if not batch.get("ok"):
                            raise BrowserError(f"BROWSER_UNTIL_ACTION_FAILED:{iteration + 1}")
                    if not matched:
                        assertion = self.assert_condition(session_id, condition)
                        matched = True
                    data = {
                        "matched": matched,
                        "iterations": len(batches),
                        "assertion": assertion,
                        "batches": batches,
                    }
                else:
                    raise ValueError(f"UNSUPPORTED_BROWSER_ACTION:{kind}")
                output.append({
                    "index": index,
                    "type": kind,
                    "ok": True,
                    "duration_ms": int((time.monotonic() - step) * 1000),
                    "data": data,
                })
            except Exception as exc:
                failed += 1
                output.append({
                    "index": index,
                    "type": kind,
                    "ok": False,
                    "duration_ms": int((time.monotonic() - step) * 1000),
                    "error": f"{type(exc).__name__}:{str(exc)[:1000]}",
                })
                if not continue_on_error:
                    break
        return {
            "session_id": self._safe_id(session_id),
            "ok": failed == 0,
            "count": len(output),
            "failed": failed,
            "results": output,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "url": self.url(session_id),
            "title": self.title(session_id),
            "position": self.position(session_id),
        }

    def scenario(
            self,
            session_id: str,
            *,
            url: str,
            actions: list[dict],
            title: str = "browser-test",
            headless: bool = False,
            screenshot: bool = True,
            auto_stop: bool = False,
            continue_on_error: bool = False,
            timeout: float = 20.0,
        ) -> dict:
            if headless:
                raise BrowserError("BROWSER_HEADLESS_FORBIDDEN_ATTACH_EXISTING_ONLY")
            started = time.monotonic()
            start_data = self.attach(session_id, timeout=timeout)
            if url:
                self.navigate(session_id, url, timeout)
            action_data = self.execute_actions(
                session_id,
                actions,
                continue_on_error=continue_on_error,
                default_timeout=min(30.0, timeout),
            )
            screenshot_data = None
            if screenshot:
                try:
                    screenshot_data = self.screenshot(
                        session_id, title=title, full_page=False
                    )
                except Exception as exc:
                    screenshot_data = {"error": f"{type(exc).__name__}:{str(exc)[:500]}"}
            evidence = {
                "schema": "easychange.browser-evidence/2",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "title": title,
                "start": start_data,
                "url": self.url(session_id),
                "page_title": self.title(session_id),
                "actions": action_data,
                "screenshot": screenshot_data,
                "passed": bool(action_data.get("ok")),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "browser_control": {
                    "protocol": "CDP",
                    "transport": "127.0.0.1 websocket",
                    "launch": "attach_existing_only",
                    "third_party_driver": False,
                    "shell_used": False,
                    "browser_launched": False,
                    "profile_created": False,
                },
            }
            if auto_stop:
                evidence["detach"] = self.stop(session_id)
            return evidence
