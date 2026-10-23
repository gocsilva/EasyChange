"""Optical AUTO view switches must cancel obsolete 32-cell raster jobs."""
from concurrent.futures import Future
import os
import pytest
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication
from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow


def test_auto_view_switch_cancels_pending_raster_without_replaying_command(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path),machine_mode=True,hid_mode=True)
    try:
        window.show()
        app.processEvents()
        window._optical_timer.stop()
        window.remote_transport_mode = "AUTO"
        window._host_optical_preference = "QR"
        old_future = Future()
        old_generation = window._optical_render_generation
        window._optical_render_pending = {
            "generation":old_generation,
            "payloads":["stale-header"],
            "index":0,
            "raster_futures":{"stale-header":old_future},
        }
        instruction = ":ec QCAN1234 :view PIXEL :state --compact"
        command = window._activate_machine_display_hint(instruction)
        assert command == ":ec QCAN1234 :state --compact"
        assert window._host_optical_preference == "PIXEL"
        assert window._optical_render_generation > old_generation
        assert old_future.cancelled(), "Obsolete QR raster Future must not delay next mode"
        assert window._optical_render_pending is None
        assert window._command_future is None
        assert not window._machine_command_queue
    finally:
        window.close()
        app.processEvents()
