from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

WINDOW_PREFIX = "EasyChange — "
SW_RESTORE = 9
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _user32():
    if os.name != "nt":
        return None
    return ctypes.windll.user32


def find_easychange_windows() -> list[int]:
    """Return visible top-level EasyChange GUI HWNDs."""
    user32 = _user32()
    if user32 is None:
        return []

    windows: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @enum_proc
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value.startswith(WINDOW_PREFIX):
            windows.append(int(hwnd))
        return True

    user32.EnumWindows(callback, 0)
    return windows


def focus_window(hwnd: int) -> bool:
    """Restore and foreground one EasyChange window."""
    user32 = _user32()
    if user32 is None or not hwnd:
        return False
    user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
    user32.BringWindowToTop(wintypes.HWND(hwnd))
    user32.SetForegroundWindow(wintypes.HWND(hwnd))
    return bool(user32.IsWindowVisible(wintypes.HWND(hwnd)))


def _tail(path: Path, limit: int = 6000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def ensure_gui(
    root: Path,
    *,
    timeout: float = 15.0,
    find_windows_fn: Callable[[], list[int]] = find_easychange_windows,
    focus_window_fn: Callable[[int], bool] = focus_window,
    popen_factory=subprocess.Popen,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict:
    root = Path(root).resolve()

    existing = find_windows_fn()
    if existing:
        hwnd = int(existing[0])
        focused = bool(focus_window_fn(hwnd))
        return {
            "ok": True,
            "action": "focused_existing",
            "hwnd": hwnd,
            "focused": focused,
        }

    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        return {
            "ok": False,
            "error": "PYTHONW_NOT_FOUND",
            "path": str(pythonw),
        }

    state_dir = root / ".easychange"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "gui-launch.log"

    before = set(find_windows_fn())
    try:
        log_handle = log_path.open("a", encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "error": "GUI_LOG_OPEN_FAILED",
            "detail": str(exc),
            "path": str(log_path),
        }

    try:
        log_handle.write(
            f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] launching EasyChange GUI\n"
        )
        log_handle.flush()
        process = popen_factory(
            [str(pythonw), "-m", "easychange.gui", ".", "--human"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=(
                DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                if os.name == "nt"
                else 0
            ),
        )

        deadline = monotonic_fn() + max(1.0, float(timeout))
        while monotonic_fn() < deadline:
            windows = find_windows_fn()
            new_windows = [hwnd for hwnd in windows if hwnd not in before]
            if new_windows:
                hwnd = int(new_windows[0])
                focused = bool(focus_window_fn(hwnd))
                return {
                    "ok": True,
                    "action": "launched",
                    "pid": int(getattr(process, "pid", 0) or 0),
                    "hwnd": hwnd,
                    "focused": focused,
                    "log": str(log_path),
                }

            return_code = process.poll()
            if return_code is not None:
                return {
                    "ok": False,
                    "error": "GUI_PROCESS_EXITED_BEFORE_WINDOW",
                    "return_code": int(return_code),
                    "log": str(log_path),
                    "log_tail": _tail(log_path),
                }
            sleep_fn(0.10)

        return {
            "ok": False,
            "error": "GUI_WINDOW_START_TIMEOUT",
            "pid": int(getattr(process, "pid", 0) or 0),
            "timeout_seconds": float(timeout),
            "log": str(log_path),
            "log_tail": _tail(log_path),
        }
    finally:
        log_handle.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = ensure_gui(Path(args.root), timeout=args.timeout)
    if result.get("ok"):
        action = result.get("action")
        if action == "focused_existing":
            print("EasyChange ja estava aberto; janela restaurada e trazida para frente.")
        else:
            print("EasyChange GUI iniciada com sucesso.")
        return 0

    print(f"[ERRO] {result.get('error')}")
    detail = result.get("detail")
    if detail:
        print(detail)
    log_tail = str(result.get("log_tail") or "").strip()
    if log_tail:
        print("\nUltimas linhas de .easychange\\gui-launch.log:")
        print(log_tail)
    log_path = result.get("log")
    if log_path:
        print(f"Log: {log_path}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
