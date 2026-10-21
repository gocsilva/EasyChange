"""Regression: slow machine commands must not block the Qt event loop."""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from easychange.core.result import Result
from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow


def _pump_until(app, predicate, seconds=4.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Qt timed out waiting for machine command")


def test_slow_hid_command_does_not_freeze_qt_and_preserves_order(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show()
    app.processEvents()
    started = threading.Event()
    release = threading.Event()
    finished = []

    def slow_execute(text):
        if not finished:
            started.set()
            if not release.wait(3.0):
                raise RuntimeError("worker was not released")
        finished.append(text)
        return Result(True, "state", {"state": "READY"})

    monkeypatch.setattr(window.service, "execute", slow_execute)
    try:
        window.command.setText(":ec QASYNCFIRST :state --compact")
        window.execute_command()
        assert started.wait(1.0)

        # An independent Qt timer can run while backend I/O is stalled.
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append("responsive"))
        app.processEvents()
        assert ticks == ["responsive"]
        assert not finished

        # Commands submitted while the backend is busy execute FIFO, not in
        # concurrent service calls (durable journal mutations stay serialized).
        window.command.setText(":ec QASYNCSECOND :state --compact")
        window.execute_command()
        assert len(window._machine_command_queue) == 1
        release.set()
        _pump_until(app, lambda: len(finished) == 2
                    and window._command_future is None
                    and not window._machine_command_queue)
        assert finished == [
            ":ec QASYNCFIRST :state --compact",
            ":ec QASYNCSECOND :state --compact",
        ]
        assert "READY" in window.output.toPlainText()
    finally:
        release.set()
        window.close()
        app.processEvents()


def test_close_defers_until_accepted_command_finishes(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show()
    app.processEvents()
    started, release = threading.Event(), threading.Event()
    def slow_execute(_text):
        started.set()
        assert release.wait(3.0)
        return Result(True, "state", {"state": "READY"})
    monkeypatch.setattr(window.service, "execute", slow_execute)
    window.command.setText(":ec QASYNCLOSE :state --compact")
    window.execute_command()
    assert started.wait(1.0)
    try:
        window.close()
        assert window._close_when_idle is True
        assert window.isVisible()
        release.set()
        _pump_until(app, lambda: not window.isVisible())
    finally:
        release.set()
        window.close()
        app.processEvents()
