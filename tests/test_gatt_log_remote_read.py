"""Read-only GATT telemetry over the same EC1 HID/HDMI channel as code tools."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from easychange.core.command_service import CommandService
from easychange.core.operation_registry import structured_operation_profile
from easychange.core.workspace import Workspace
from easychange.remote.gatt_diagnostics import GattDiagnosticLog
from easychange.ui.main_window import MainWindow


def test_easychange_ec1_gatt_log_is_live_readonly_and_sequence_durable(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    trace = GattDiagnosticLog()
    try:
        trace.record("BUSCA_BLE", "Tentativa #1")
        trace.record("INSCRICAO_GATT", "ATT 0x03 Write Not Permitted")
        service.gatt_diagnostics_source = trace.snapshot
        first = service.execute(":ec QGATTLOG001 :gatt-log 0 2")
        assert first.ok
        assert first.sequence == "QGATTLOG001"
        assert first.data["available"] is True
        assert len(first.data["events"]) == 2
        assert first.data["events"][-1]["stage"] == "INSCRICAO_GATT"
        cursor = first.data["cursor"]
        # No accidental replay with the old sequence: a new EC1 sequence is
        # required for each polling read to avoid serving a stale snapshot.
        trace.record("DESCONECTADO", "Nova tentativa")
        old = service.execute(":ec QGATTLOG001 :gatt-log 0 2")
        assert old.ok and len(old.data["events"]) == 2
        next_result = service.execute(
            f":ec QGATTLOG002 :gatt-log {cursor} 8")
        assert next_result.ok and len(next_result.data["events"]) == 1
        assert next_result.data["events"][0]["stage"] == "DESCONECTADO"
        assert service.execute(":gatt-log 1 1").ok
    finally:
        service.close()


def test_gatt_structured_tail_is_readonly_and_bounded(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    trace = GattDiagnosticLog()
    try:
        trace.record("CONEXAO_BLE", "Link pronto")
        service.gatt_diagnostics_source = trace.snapshot
        cmd = ':j1 {"op":"operation","type":"gatt_diagnostics_tail","after":0,"limit":4}'
        result = service.execute(":ec QGATTJ1001 " + cmd)
        assert result.ok and result.data["available"]
        assert len(result.data["events"]) == 1
        assert structured_operation_profile("gatt_diagnostics_tail")["side_effect"] is False
        assert structured_operation_profile("gatt_diagnostics_tail")["continue_on_error_safe"]
        bad = service.execute(":ec QGATTJ1002 :j1 " +
                              '{"op":"operation","type":"gatt_diagnostics_tail","after":false}')
        assert not bad.ok
        assert bad.code == "INVALID_GATT_LOG_REQUEST"
    finally:
        service.close()


def test_gatt_log_missing_gui_returns_explicit_not_available(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        result = service.execute(":gatt-log")
        assert result.ok
        assert result.data == {
            "available": False, "reason": "GUI_GATT_LOG_UNAVAILABLE",
            "events": [], "cursor": 0, "oldest": 0, "dropped": 0,
        }
        assert not service.execute(":gatt-log -4 2").ok
        assert not service.execute(":gatt-log 0 999").ok
    finally:
        service.close()


def test_remote_qt_mainwindow_wires_gatt_snapshot_to_core(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=False, hid_mode=False)
    window.show()
    app.processEvents()
    try:
        window._gatt_client.diagnostics.record("INSCRICAO_GATT",
                                               "Write Not Permitted handle=111")
        result = window.service.execute(":gatt-log 0 8")
        assert result.ok and result.data["available"]
        assert any("handle=111" in event["message"] for event in result.data["events"])
        # Local diagnostics remain available without starting a BLE worker.
        assert window._gatt_client._thread is None
        assert not window._gatt_timer.isActive()
    finally:
        window.close()
        app.processEvents()
