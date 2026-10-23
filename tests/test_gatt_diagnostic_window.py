"""Qt popup live BLE GATT diagnostics, user copy/save and redacted trace."""
from __future__ import annotations

import os
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from easychange.core.workspace import Workspace
from easychange.remote.gatt_diagnostics import GattDiagnosticLog
from easychange.ui.gatt_diagnostic_window import GattDiagnosticWindow
from easychange.ui.main_window import MainWindow


def _app():
    return QApplication.instance() or QApplication([])


def test_threadsafe_bounded_trace_timestamp_redaction_and_cursor():
    log = GattDiagnosticLog(max_events=60)
    log.record("PAIRING", "Authorization: Bearer ABCDE123456 and token=secret999")
    def writer():
        for i in range(100):
            log.record("CONEXAO_BLE", f"attempt={i}")
    t = threading.Thread(target=writer)
    t.start()
    t.join(5)
    assert not t.is_alive()
    all_items = log.snapshot(0, limit=200)["events"]
    assert len(all_items) == 60
    assert all(item["timestamp"].count(":") >= 2 for item in all_items)
    assert all("ABCDE123456" not in item["message"] for item in all_items)
    assert all("secret999" not in item["message"] for item in all_items)
    first = log.snapshot(0, limit=7)
    second = log.snapshot(first["cursor"], limit=7)
    assert len(first["events"]) == 7
    assert second["events"][0]["id"] > first["cursor"]


def test_full_traceback_saved_but_no_code_payload_or_keys():
    log = GattDiagnosticLog()
    try:
        raise RuntimeError("GATT Protocol Error: Write Not Permitted (0x03)")
    except RuntimeError as exc:
        log.record("INSCRICAO_GATT", "Acesso ao CCCD recusado", exc=exc)
    item = log.snapshot()["events"][0]["message"]
    assert "Traceback" in item and "RuntimeError" in item
    assert "0x03" in item and "Acesso ao CCCD" in item
    assert len(item) < 8000


def test_separate_window_autoscroll_single_trace_copy_and_save(tmp_path, monkeypatch):
    app = _app()
    trace = GattDiagnosticLog()
    trace.record("INICIO", "Iniciou")
    win = GattDiagnosticWindow()
    win.show()
    app.processEvents()
    try:
        win.refresh(trace)
        assert "Iniciou" in win.log_text.toPlainText()
        assert "INICIO" in win.log_text.toPlainText()
        trace.record("FALHA", "Write Not Permitted - CCCD")
        win.refresh(trace)
        text = win.log_text.toPlainText()
        assert text.count("Iniciou") == 1
        assert "Write Not Permitted" in text
        assert win.log_text.isReadOnly()
        assert win.log_text.verticalScrollBar().value() == win.log_text.verticalScrollBar().maximum()
        win.copy_button.click()
        assert app.clipboard().text() == text
        dest = tmp_path / "diagnostico.txt"
        monkeypatch.setattr(
            "easychange.ui.gatt_diagnostic_window.QFileDialog.getSaveFileName",
            lambda *_a, **_kw: (str(dest), "Arquivo texto (*.txt)"),
        )
        win.save_button.click()
        assert dest.read_text(encoding="utf-8") == text
    finally:
        win.close()


def test_main_window_button_opens_only_one_live_popup_and_starts_no_extra_gatt(tmp_path):
    app = _app()
    win = MainWindow(Workspace.open(tmp_path), machine_mode=False, hid_mode=False)
    win.show()
    app.processEvents()
    try:
        assert win.gatt_diagnostic_button.text() == "Diagnóstico GATT"
        assert win.findChild(type(win.gatt_diagnostic_button),
                             "openGattDiagnosticButton") is win.gatt_diagnostic_button
        assert win._gatt_diagnostic_window is None
        win._gatt_client.diagnostics.record(
            "INSCRICAO_GATT", "BleakGATTProtocolError 0x03 notify=1 cccd=1 handle=111")
        original_worker = win._gatt_client._thread
        win.gatt_diagnostic_button.click()
        app.processEvents()
        dialog = win._gatt_diagnostic_window
        assert dialog is not None and dialog.isVisible()
        assert "handle=111" in dialog.log_text.toPlainText()
        win.gatt_diagnostic_button.click()
        assert win._gatt_diagnostic_window is dialog
        assert win._gatt_client._thread is original_worker
        win._gatt_client.diagnostics.record("BUSCA_BLE", "Nova tentativa")
        win._poll_gatt_client()
        assert "Nova tentativa" in dialog.log_text.toPlainText()
        dialog.close()
        assert not dialog.isVisible()
        win.gatt_diagnostic_button.click()
        assert win._gatt_diagnostic_window is dialog
        assert dialog.isVisible()
    finally:
        win.close()
        app.processEvents()



def test_main_window_gatt_retry_button_is_non_admin_and_does_not_restart_esp(tmp_path, monkeypatch):
    app = _app()
    window = MainWindow(Workspace.open(tmp_path), machine_mode=False, hid_mode=False)
    window.show()
    app.processEvents()
    try:
        attempts = []
        monkeypatch.setattr(
            window._gatt_client, "retry_now",
            lambda: (attempts.append("worker_signal"), True)[1])
        # GATT no longer starts on application launch.
        monkeypatch.setattr(window._gatt_client, "start", lambda: None)
        assert not window.gatt_retry_button.isEnabled()
        window.gatt_toggle_button.click()
        assert window.gatt_retry_button.text() == "Tentar GATT"
        assert window.gatt_retry_button.objectName() == "retryGattConnectionButton"
        window.gatt_retry_button.click()
        assert attempts == ["worker_signal"]
    finally:
        window.close()
        app.processEvents()
