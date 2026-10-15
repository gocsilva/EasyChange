"""Small sequenced native reads are ready for optical paint without EC2 worker delay."""
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("qrcode")

from PySide6.QtWidgets import QApplication
from easychange.core.result import Result
from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow


def test_small_read_payload_available_synchronously(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    try:
        result = Result(
            True, "read", {"path": "src/Foo.cs", "lines": ["hello", "world"]},
            sequence="QINLINEFAST42",
        )
        window._update_machine_qr(result)
        assert window._optical_encode_future is None
        assert window._optical_payloads
        app.processEvents()
    finally:
        window.close()
        app.processEvents()


def test_larger_read_retains_async_worker(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    try:
        result = Result(
            True, "read", {"path": "src/Foo.cs", "lines": ["hello"] * 13},
            sequence="QLONGREAD42",
        )
        window._update_machine_qr(result)
        assert window._optical_encode_future is not None
    finally:
        window.close()
        app.processEvents()
