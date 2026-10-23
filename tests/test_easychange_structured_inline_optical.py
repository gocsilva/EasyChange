"""Small EC1 structured reads must paint their EC2 receipt on the first try.

Purely local offscreen Qt tests: no ESP, HDMI device or remote PC is used.
"""
import os
from concurrent.futures import Future

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from easychange.core.result import Result
from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow


def _result(value: str, inner: str):
    primary = {"ok": True, "command": inner, "data": {"value": value}}
    return Result(
        True, "structured", sequence="QINLINE1",
        data={
            "schema": "easychange.structured/2",
            "operation": "operation",
            "results": [primary], "primary": primary, "count": 1,
        },
    )


@pytest.mark.parametrize("kind", ["state", "command_schema", "discover_many", "health"])
def test_bounded_structured_read_encodes_immediately_without_async_queue(
    tmp_path, monkeypatch, kind,
):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    try:
        window.show()
        app.processEvents()
        def forbidden(*_args, **_kwargs):
            pytest.fail("bounded structured read must not wait for background future")
        monkeypatch.setattr(window._optical_encode_executor, "submit", forbidden)
        window._update_machine_qr(_result("READY", kind))
        # Synchronous Future is consumed and cleared by _poll_optical_bundle.
        assert window._optical_encode_future is None
        assert window._optical_payloads
        assert window._optical_render_pending is None
    finally:
        window.close()
        app.processEvents()


def test_large_structured_result_still_uses_nonblocking_encoder(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    pending = Future()
    called = []
    try:
        window.show()
        app.processEvents()
        def submit(*_args, **_kwargs):
            called.append(True)
            return pending
        monkeypatch.setattr(window._optical_encode_executor, "submit", submit)
        window._update_machine_qr(_result("Z" * 20000, "discover_many"))
        assert called == [True]
        assert window._optical_encode_future is pending
    finally:
        pending.cancel()
        window.close()
        app.processEvents()
