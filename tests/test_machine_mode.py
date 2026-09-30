import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("qrcode")

from PySide6.QtWidgets import QApplication

from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow


def test_machine_mode_keeps_command_focus_and_renders_sequence_qr(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()
    window._run_command(":ec QGUI1234 :state"); app.processEvents()
    assert window.output.toPlainText().startswith("EC1 QGUI1234 OK")
    assert window.machine_qr.isVisible()
    assert window.machine_qr.pixmap() is not None

    window.editor.setFocus(); app.processEvents()
    app.processEvents()
    assert window.command.hasFocus()
    window.close(); app.processEvents()
