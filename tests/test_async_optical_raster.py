"""Qt stays responsive while 32 lossless EC2 QR codes are rasterized.

This exercises the real page scheduler locally; it never touches USB HID or
the HDMI remote computer and never degrades QR/ECP1 matrix resolution.
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import easychange.ui.main_window as gui
from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow


def test_32_code_page_keeps_qt_pumping_during_slow_qr_raster(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    main_thread = threading.get_ident()
    worker_threads = set()
    original = gui.rasterize_optical_payload

    def slow_raster(*args, **kwargs):
        if args and '"s":"Q32ASYNC"' in str(args[0]):
            worker_threads.add(threading.get_ident())
        time.sleep(0.025)  # synthetic CPU/driver delay; must be off the GUI thread
        return original(*args, **kwargs)

    monkeypatch.setattr(gui, "rasterize_optical_payload", slow_raster)
    try:
        window.show()
        app.processEvents()
        values = [
            json.dumps({"p":"EC2","s":"Q32ASYNC","n":32,
                        "i":i,"d":"TEST"+str(i)+"X"*35},
                       separators=(",",":"))
            for i in range(32)
        ]
        # Discard the startup receipt first: it may still have an active
        # render job from machine-mode initialization.
        window._optical_timer.stop()
        window._discard_optical_render_jobs()
        window._optical_render_generation += 1
        window._optical_render_pending = None
        window._optical_pixmaps.clear()
        window._optical_payloads = values
        window._optical_header_payload = None
        window._set_optical_burst(True)
        window._optical_page = 0
        window._render_optical_page()

        responsive = []
        QTimer.singleShot(0, lambda: responsive.append(threading.get_ident()))
        QTest.qWait(35)
        assert responsive == [main_thread]
        assert window._optical_render_pending is not None  # work still running
        assert worker_threads
        assert all(t != main_thread for t in worker_threads)

        deadline = time.monotonic() + 8
        while window._optical_render_pending is not None and time.monotonic() < deadline:
            QTest.qWait(20)
        assert window._optical_render_pending is None
        assert all(label.pixmap() is not None for label in window.machine_qrs)
        assert len({p.cacheKey() for p in window._optical_pixmaps.values()}) >= 32
        assert all(window._optical_rendered_wire[p][1] == "QR" for p in values)
        assert all(window._optical_rendered_wire[p][0] == p for p in values)
    finally:
        window.close()
        app.processEvents()


def test_lossless_qr_and_pixel_raster_keep_original_ec2_data():
    from easychange.remote.optical_raster import rasterize_optical_payload
    from easychange.remote.pixel_transport import SIDE
    from easychange.remote.pixel_transport import decode_pixel_bitmap
    raw = json.dumps({"p":"EC2","s":"QSAME","i":1,"n":3,
                      "d":"A"*80}, separators=(",",":"))
    for mode in ("QR", "PIXEL", "HYBRID"):
        pixels, side, kind, wire = rasterize_optical_payload(
            raw, mode, "QR", burst=True, label_side=125
        )
        assert wire == raw
        assert pixels and len(pixels) == side * side
        if kind == "PIXEL":
            assert side == SIDE
            assert decode_pixel_bitmap(pixels) == raw
        else:
            assert kind == "QR"
