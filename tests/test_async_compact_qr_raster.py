"""Ordinary 1-4-cell EC2 QR pages must not block the Qt event loop."""
import json
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import easychange.ui.main_window as gui
from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow


@pytest.mark.parametrize("count,payload_chars", [(3, 90), (1, 600)])
def test_compact_qr_raster_runs_off_gui_thread(tmp_path, monkeypatch, count, payload_chars):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    qt_thread = threading.get_ident()
    rendered_on = []
    original = gui.rasterize_optical_payload

    def slow_raster(*args, **kwargs):
        rendered_on.append(threading.get_ident())
        time.sleep(0.08)
        return original(*args, **kwargs)

    monkeypatch.setattr(gui, "rasterize_optical_payload", slow_raster)
    try:
        window.show()
        app.processEvents()
        window._optical_timer.stop()
        window._discard_optical_render_jobs()
        window._optical_render_generation += 1
        window._optical_render_pending = None
        window._optical_pixmaps.clear()
        wire = [
            json.dumps(
                {"p": "EC2", "s": "QCOMPACTASYNC", "n": count, "i": i,
                 "d": "X" * payload_chars}, separators=(",", ":")
            )
            for i in range(count)
        ]
        window._optical_payloads = wire
        window._optical_header_payload = None
        window._set_optical_burst(False)
        window._render_optical_page()

        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(threading.get_ident()))
        QTest.qWait(30)
        assert ticks == [qt_thread]
        assert rendered_on
        assert all(thread != qt_thread for thread in rendered_on)

        deadline = time.monotonic() + 5
        while window._optical_render_pending is not None and time.monotonic() < deadline:
            QTest.qWait(20)
        assert window._optical_render_pending is None
        assert set(wire) == set(window._optical_pixmaps)
        assert all(window._optical_rendered_wire[p][0] == p for p in wire)
        assert all(window._optical_rendered_wire[p][1] == "QR" for p in wire)
    finally:
        window.close()
        app.processEvents()


def test_single_short_receipt_keeps_immediate_inline_raster(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    qt_thread = threading.get_ident()
    rendered_on = []
    original = gui.rasterize_optical_payload

    def observe_raster(*args, **kwargs):
        rendered_on.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(gui, "rasterize_optical_payload", observe_raster)
    try:
        window.show()
        app.processEvents()
        window._optical_timer.stop()
        window._discard_optical_render_jobs()
        window._optical_render_generation += 1
        window._optical_render_pending = None
        window._optical_pixmaps.clear()
        receipt = '{"p":"EC2H","s":"QINLINEASYNC","n":1,"w":1}'
        window._optical_payloads = [receipt]
        window._optical_header_payload = None
        window._set_optical_burst(False)
        window._render_optical_page()
        app.processEvents()
        assert qt_thread in rendered_on
        assert receipt in window._optical_pixmaps
    finally:
        window.close()
        app.processEvents()
