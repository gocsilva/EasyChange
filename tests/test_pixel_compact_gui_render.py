"""Exercise Qt's actual PIXEL renderer in a headless compact panel."""
import json
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtGui import QImage

from easychange.ui.main_window import MainWindow
from easychange.remote.pixel_transport import (
    LARGE_SIDE, decode_pixel_bitmap,
)


def test_compact_renderer_uses_72_cell_bitmap_instead_of_qr():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    app = QApplication.instance() or QApplication([])
    label = QLabel()
    label.setFixedSize(210, 210)
    recorded = []
    fake = SimpleNamespace(
        remote_transport_mode="PIXEL",
        _host_optical_preference="QR",
        _optical_burst=False,
        machine_qrs=[label],
        _remember_optical_wire=lambda original, encoded, kind: recorded.append(
            (original, encoded, kind)
        ),
    )
    packet = json.dumps({
        "p": "EC2", "v": 2, "s": "QRENDER72", "i": 0, "n": 1,
        "h": "a" * 16, "c": "00000000", "x": "00000000",
        "d": "Q" * 220,
    }, separators=(",", ":"))
    pixmap = MainWindow._build_optical_pixmap(fake, packet)
    assert not pixmap.isNull()
    assert recorded[-1][2] == "PIXEL"
    img = pixmap.toImage().convertToFormat(QImage.Format.Format_Grayscale8)
    raw = np.frombuffer(img.bits(), dtype=np.uint8, count=img.sizeInBytes())
    gray = raw.reshape(img.height(), img.bytesPerLine())[:, :img.width()]
    compact = cv2.resize(gray, (LARGE_SIDE, LARGE_SIDE),
                         interpolation=cv2.INTER_AREA)
    visual = decode_pixel_bitmap(compact.ravel(), side=LARGE_SIDE)
    assert json.loads(visual)["d"] == "Q" * 220
    assert json.loads(visual)["tm"] == "PIXEL"
    assert json.loads(visual)["rm"] == "PIXEL"


def test_dense_burst_preserves_56_cell_limit_and_qr_escape_hatch():
    app = QApplication.instance() or QApplication([])
    label = QLabel()
    label.setFixedSize(125, 125)
    kinds = []
    fake = SimpleNamespace(
        remote_transport_mode="PIXEL",
        _host_optical_preference="QR",
        _optical_burst=True,
        machine_qrs=[label],
        _remember_optical_wire=lambda original, encoded, kind: kinds.append(kind),
    )
    packet = json.dumps({"p": "EC2", "s": "QTEST", "d": "Z" * 390})
    pixmap = MainWindow._build_optical_pixmap(fake, packet)
    assert not pixmap.isNull()
    assert kinds[-1] == "QR"  # no excessive ECP1 density at 720p/32-cells
