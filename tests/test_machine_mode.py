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



def test_machine_mode_uses_eight_optical_slots_and_hides_file_tree(tmp_path):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "large.py"
    source.write_text(
        "\n".join(f"value_{i} = '{i:04d}-{i * 7919:08d}'" for i in range(500)),
        encoding="utf-8",
    )
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()
    window._run_command(":ec QGUI5678 :read large.py 1 500")
    app.processEvents()
    assert not window.tree.isVisible()
    assert len(window.machine_qrs) == 8
    assert len(window._optical_payloads) > 8
    assert all(label.pixmap() is not None for label in window.machine_qrs)
    assert all(label.width() == 160 and label.height() == 160 for label in window.machine_qrs)
    window.close(); app.processEvents()



def test_machine_mode_reuses_cached_qr_pixmaps(tmp_path):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "large.py"
    source.write_text(
        "\n".join(f"value_{i} = '{i:04d}-{i * 15485863:08d}'" for i in range(500)),
        encoding="utf-8",
    )
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()
    window._run_command(":ec QCACHE123 :read large.py 1 500")
    app.processEvents()
    first_payload = window._optical_payloads[0]
    first_pixmap = window._optical_pixmaps[first_payload]
    window._optical_page = 0
    window._render_optical_page()
    app.processEvents()
    assert window._optical_pixmaps[first_payload].cacheKey() == first_pixmap.cacheKey()
    window.close(); app.processEvents()
