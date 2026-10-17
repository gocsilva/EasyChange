from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts import launch_easychange_gui as launcher


def test_gui_launcher_focuses_existing_window_without_starting_process(tmp_path):
    calls = {"focus": [], "popen": 0}

    def popen(*args, **kwargs):
        calls["popen"] += 1
        raise AssertionError("process must not start when a GUI window already exists")

    result = launcher.ensure_gui(
        tmp_path,
        find_windows_fn=lambda: [12345],
        focus_window_fn=lambda hwnd: calls["focus"].append(hwnd) or True,
        popen_factory=popen,
    )

    assert result["ok"] is True
    assert result["action"] == "focused_existing"
    assert calls["focus"] == [12345]
    assert calls["popen"] == 0


def test_gui_launcher_waits_for_real_window_and_focuses_it(tmp_path, monkeypatch):
    pythonw = tmp_path / ".venv" / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.write_bytes(b"stub")

    windows = iter([[], [], [9876]])
    captured = {}

    class Process:
        pid = 4321

        @staticmethod
        def poll():
            return None

    def popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return Process()

    clock = {"value": 0.0}

    def monotonic():
        clock["value"] += 0.05
        return clock["value"]

    result = launcher.ensure_gui(
        tmp_path,
        timeout=2.0,
        find_windows_fn=lambda: next(windows),
        focus_window_fn=lambda hwnd: captured.setdefault("focused", hwnd) == hwnd,
        popen_factory=popen,
        sleep_fn=lambda _seconds: None,
        monotonic_fn=monotonic,
    )

    assert result["ok"] is True
    assert result["action"] == "launched"
    assert result["hwnd"] == 9876
    assert result["pid"] == 4321
    assert captured["argv"][-3:] == ["-m", "easychange.gui", "--human"]
    assert captured["focused"] == 9876


def test_gui_launcher_reports_child_exit_before_window(tmp_path):
    pythonw = tmp_path / ".venv" / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.write_bytes(b"stub")

    process = SimpleNamespace(pid=99, poll=lambda: 7)

    result = launcher.ensure_gui(
        tmp_path,
        timeout=1.0,
        find_windows_fn=lambda: [],
        focus_window_fn=lambda hwnd: True,
        popen_factory=lambda *args, **kwargs: process,
        sleep_fn=lambda _seconds: None,
    )

    assert result["ok"] is False
    assert result["error"] == "GUI_PROCESS_EXITED_BEFORE_WINDOW"
    assert result["return_code"] == 7


def test_gui_launcher_reports_missing_pythonw(tmp_path):
    result = launcher.ensure_gui(
        tmp_path,
        find_windows_fn=lambda: [],
    )
    assert result["ok"] is False
    assert result["error"] == "PYTHONW_NOT_FOUND"
