"""Live PIXEL+QR routing and EC2 dwell in the *same* Qt session."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("qrcode")

from PySide6.QtWidgets import QApplication
from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow


def test_hybrid_renders_both_codecs_without_new_command(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    try:
        window.show()
        app.processEvents()
        window._set_remote_transport_mode("PIXEL+QR")
        assert window.remote_transport_mode == "PIXEL+QR"
        assert window._pixel_chunk_chars() == 130
        sequence = "QHYBRID32"
        header = json.dumps({"p": "EC2H", "s": sequence, "n": 2, "w": 2})
        chunk0 = json.dumps({"p": "EC2", "s": sequence, "i": 0, "n": 2, "d": "AA"})
        chunk1 = json.dumps({"p": "EC2", "s": sequence, "i": 1, "n": 2, "d": "BB"})
        for value in (header, chunk0, chunk1):
            assert window._build_optical_pixmap(value).isNull() is False
        assert window._optical_rendered_wire[header][1] == "PIXEL"
        assert window._optical_rendered_wire[chunk0][1] == "QR"
        assert window._optical_rendered_wire[chunk1][1] == "PIXEL"
        assert len(window._machine_command_queue) == 0
    finally:
        window.close()
        app.processEvents()


def test_live_auto_hint_changes_dwell_and_view_without_relaunch(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    try:
        window.show()
        app.processEvents()
        monkeypatch.setattr(window, "_start_next_machine_command", lambda: None)
        window.service.configure_optical_transport(
            adaptive_enabled=True, min_ms=600, max_ms=1800, current_ms=900
        )
        window._queue_machine_command(
            ":ec QLIVE001 :view HYBRID :dwell 700 :state --compact"
        )
        assert window.remote_transport_mode == "AUTO"
        assert window._host_optical_preference == "QR"
        assert window._machine_command_queue[-1] == (
            ":ec QLIVE001 :view HYBRID :dwell 700 :state --compact"
        )
        assert window.service.optical_transport_config()["current_ms"] == 900
        # Only the dequeued command is allowed to change the live view.
        assert window._activate_machine_display_hint(
            window._machine_command_queue.popleft()
        ) == ":ec QLIVE001 :state --compact"
        assert window._host_optical_preference == "HYBRID"
        assert window.service.optical_transport_config()["current_ms"] == 700
        window._set_remote_transport_mode("QR")
        window._queue_machine_command(
            ":ec QLIVE002 :view PIXEL :dwell 850 :state --compact"
        )
        assert window.remote_transport_mode == "QR"
        assert window._machine_command_queue[-1] == (
            ":ec QLIVE002 :view PIXEL :dwell 850 :state --compact"
        )
        assert window._activate_machine_display_hint(
            window._machine_command_queue.popleft()
        ) == ":ec QLIVE002 :state --compact"
        assert window.service.optical_transport_config()["current_ms"] == 850
    finally:
        window.close()
        app.processEvents()


def test_manual_dwell_ignores_mcp_adaptation(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    try:
        monkeypatch.setattr(window, "_start_next_machine_command", lambda: None)
        window.service.configure_optical_transport(
            adaptive_enabled=False, current_ms=1100
        )
        window._queue_machine_command(
            ":ec QMANUAL1 :view QR :dwell 650 :state --compact"
        )
        assert window.service.optical_transport_config()["current_ms"] == 1100
        assert window._activate_machine_display_hint(
            window._machine_command_queue.popleft()
        ) == ":ec QMANUAL1 :state --compact"
        assert window.service.optical_transport_config()["current_ms"] == 1100
    finally:
        window.close()
        app.processEvents()


def test_queued_hint_cannot_replace_previous_optical_receipt(tmp_path):
    """An in-flight :find result stays visible when an AUTO probe arrives."""
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    try:
        window._command_future = object()  # prior command still processing
        generation = window._optical_render_generation
        previous_mode = window._host_optical_preference
        window.service.configure_optical_transport(
            adaptive_enabled=True, current_ms=900
        )
        window._queue_machine_command(
            ":ec QNEXT222 :view PIXEL :dwell 650 :state --compact"
        )
        assert window._optical_render_generation == generation
        assert window._host_optical_preference == previous_mode
        assert window.service.optical_transport_config()["current_ms"] == 900
        assert window._machine_command_queue[-1].startswith(":ec QNEXT222 :view PIXEL")
        # Dequeuing later strips only the display envelope, preserving EC1.
        command = window._activate_machine_display_hint(
            window._machine_command_queue.popleft()
        )
        assert command == ":ec QNEXT222 :state --compact"
        assert window._host_optical_preference == "PIXEL"
        assert window.service.optical_transport_config()["current_ms"] == 650
    finally:
        window._command_future = None
        window.close()
        app.processEvents()
