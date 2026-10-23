"""EC2 QR recording controls and GUI display manifest are deterministic."""
import hashlib
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from easychange.core.optical_test_counter import OpticalTestCounter
from easychange.core.command_service import CommandService
from easychange.core.operation_registry import structured_operation_is_mutation
from easychange.core.workspace import Workspace


SID = "a" * 32


def test_display_counter_exact_hashes_and_repeated_slots():
    counter = OpticalTestCounter()
    p0 = json.dumps({"p": "EC2", "s": "QREC1", "i": 0})
    p1 = json.dumps({"p": "EC2H", "s": "QREC1", "f": True})
    assert counter.start(SID)["state"] == "RUNNING"
    counter.record([p0,p1,p0,"not-a-qr"])
    summary = counter.pause(SID)
    assert summary["qr_display_events"] == 3
    assert summary["pages_presented"] == 1
    assert summary["qr_unique"] == 2
    manifest = counter.status(SID, limit=1)
    assert manifest["next_cursor"] == 1
    rows = manifest["manifest"] + counter.status(SID, cursor=1)["manifest"]
    assert set(v["sha256"] for v in rows) == {
        hashlib.sha256(p.encode("utf-8")).hexdigest() for p in (p0,p1)
    }
    counter.record([json.dumps({"p":"EC2","s":"QAFTER"})])
    assert counter.status(SID)["qr_display_events"] == 3


def test_session_validation_and_no_hidden_other_session():
    counter = OpticalTestCounter()
    with pytest.raises(ValueError, match="INVALID_QR_TEST_SESSION"):
        counter.start("../../outside")
    counter.start(SID)
    with pytest.raises(ValueError, match="QR_TEST_ALREADY_RUNNING"):
        counter.start("b"*32)
    with pytest.raises(ValueError, match="QR_TEST_SESSION_NOT_FOUND"):
        counter.pause("b"*32)
    counter.pause(SID)


def test_optical_controls_preserve_idempotent_sequence_policy():
    assert structured_operation_is_mutation({"type":"optical_qr_record","action":"start"})
    assert structured_operation_is_mutation({"type":"optical_qr_record","action":"pause"})
    assert not structured_operation_is_mutation({"type":"optical_qr_record","action":"status"})


def test_structured_gui_recorder_roundtrip(tmp_path):
    service = CommandService(Workspace.open(tmp_path), defer_initial_maintenance=True)
    counter = OpticalTestCounter()
    service.optical_test_counter = counter
    def send(seq, action, **extra):
        item = {"op":"operation","type":"optical_qr_record",
                "session_id":SID,"action":action,**extra}
        return service.execute(":ec "+seq+" :j1 "+json.dumps(item))
    try:
        assert send("QTESTSTART1","start").ok
        counter.record([json.dumps({"p":"EC2","s":"Q1","i":0})])
        assert send("QTESTPAUSE1","pause").data["qr_unique"] == 1
        result = send("QTESTSTATUS1","status")
        assert result.ok and len(result.data["manifest"]) == 1
        assert result.data["state"] == "PAUSED"
        assert send("QTESTBAD123","status", cursor=-1).code == "INVALID_QR_TEST_CURSOR"
    finally:
        service.close()

def test_real_qt_page_commit_is_counted_after_pixmap_assignment(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from easychange.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    try:
        window._optical_test_counter.start(SID)
        qr = json.dumps({"p":"EC2", "s":"QCOMMIT1", "i":0})
        window._optical_payloads = [qr]
        window._optical_header_payload = None
        window._optical_render_generation += 1
        window._render_optical_page()
        for _ in range(16):
            app.processEvents()
            if window._optical_render_pending is None:
                break
        assert window._optical_render_pending is None
        snap = window._optical_test_counter.pause(SID)
        assert snap["qr_display_events"] == 1
        assert snap["qr_unique"] == 1
    finally:
        window.close()
        app.processEvents()

def test_explicit_qr_selector_hashes_actual_painted_wire(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from easychange.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    try:
        window.remote_transport_mode = "QR"
        window._optical_test_counter.start(SID)
        raw = json.dumps({"p":"EC2","s":"QACTUALQR1","i":0},separators=(",",":"))
        window._optical_payloads = [raw]
        window._optical_header_payload = None
        window._optical_render_generation += 1
        window._render_optical_page()
        for _ in range(30):
            app.processEvents()
            if window._optical_render_pending is None:
                break
        assert window._optical_render_pending is None
        result = window._optical_test_counter.status(SID)
        # Hash the actual on-screen wire representation. This includes
        # versioned transport hints such as vh=2, not just the old tm/rm
        # metadata. The recorder must match exactly what Qt painted.
        painted_wire, kind = window._optical_rendered_wire[raw]
        painted = json.loads(painted_wire)
        assert kind == "QR"
        # Mode metadata belongs only to EC2H; data packets must be byte-for-
        # byte unchanged to keep dense 32-code pages readable on HDMI 720p.
        assert painted_wire == raw
        assert "tm" not in painted and "rm" not in painted
        assert result["manifest"][0]["sha256"] == hashlib.sha256(
            painted_wire.encode("utf-8")
        ).hexdigest()
        assert result["non_qr_displays"] == 0
    finally:
        window.close()
        app.processEvents()
