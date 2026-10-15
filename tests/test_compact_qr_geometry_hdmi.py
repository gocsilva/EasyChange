"""Optical compact QR geometry under HDMI scaling (UI-only tests)."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PySide6")
pytest.importorskip("qrcode")

from PySide6.QtWidgets import QApplication
from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow


def test_compact_qr_geometry_is_larger_and_preserves_32_slot_burst(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    try:
        window._set_optical_burst(False)
        assert window._OPTICAL_COMPACT_QR_SIZE == 260
        assert window._OPTICAL_BURST_SLOTS == 32
        assert all(
            label.width() == 260 and label.height() == 260
            for label in window.machine_qrs
        )
        assert window.result_panel.maximumHeight() >= 260
        pixmap = window._build_optical_pixmap('{"p":"EC2H","s":"QCOMPACT"}')
        assert pixmap.width() == 260
        assert pixmap.height() == 260
    finally:
        window.close()
        app.processEvents()
