"""A stale EC2 encode must not block the next optical result."""
import json
from threading import Event

from PySide6.QtWidgets import QApplication

from easychange.core.result import Result
from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow


def test_new_optical_command_encodes_while_stale_future_still_running(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    first_started = Event()
    release_first = Event()

    def encode(result, **_kwargs):
        if result.sequence == "QOLDENCODE":
            first_started.set()
            if not release_first.wait(3.0):
                raise TimeoutError("STUCK_OLD_ENCODING")
        payload = json.dumps(
            {"p": "EC2", "s": result.sequence, "i": 0, "n": 1, "d": "x"},
            separators=(",", ":"),
        )
        return [payload], None

    monkeypatch.setattr("easychange.ui.main_window.encode_result_bundle", encode)
    try:
        window._update_machine_qr(
            Result(True, "read", {"value": "old"}, sequence="QOLDENCODE")
        )
        assert first_started.wait(1.0)
        old_future = window._optical_encode_future
        assert not old_future.done()

        window._update_machine_qr(
            Result(True, "read", {"value": "new"}, sequence="QNEWENCODE")
        )
        new_future = window._optical_encode_future
        # New payload must complete without releasing the first worker.
        payloads, _ = new_future.result(timeout=1.5)
        assert "QNEWENCODE" in payloads[0]
        assert not old_future.done()
        window._poll_optical_bundle(window._optical_render_generation, new_future)
        assert window._optical_payloads == payloads
    finally:
        release_first.set()
        window.close()
        app.processEvents()
