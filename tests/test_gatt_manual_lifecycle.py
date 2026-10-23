"""GATT is opt-in and fully released on operator shutdown."""
from __future__ import annotations

import asyncio
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from easychange.core.workspace import Workspace
from easychange.remote.gatt_client import EasyChangeGattClient
from easychange.ui.main_window import MainWindow


def _app():
    return QApplication.instance() or QApplication([])


def test_gui_never_starts_gatt_until_operator_enables_it(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(EasyChangeGattClient, "start", lambda self: calls.append("start"))
    monkeypatch.setattr(EasyChangeGattClient, "retry_now", lambda self: calls.append("retry") or True)
    monkeypatch.setattr(EasyChangeGattClient, "stop",
                        lambda self, timeout=3.0: calls.append("stop") or True)
    app = _app()
    win = MainWindow(Workspace.open(tmp_path), machine_mode=False)
    app.processEvents()
    try:
        assert calls == []
        assert win._gatt_client._thread is None
        assert not win._gatt_timer.isActive()
        assert win.gatt_toggle_button.text() == "Ligar GATT"
        assert not win.gatt_retry_button.isEnabled()
        win.open_gatt_diagnostics()
        assert calls == []
        win.gatt_toggle_button.click()
        assert calls == ["start"]
        assert win._gatt_timer.isActive()
        assert win.gatt_retry_button.isEnabled()
        assert win.gatt_toggle_button.text() == "Desligar GATT"
        win.gatt_retry_button.click()
        assert calls == ["start", "retry"]
        win.gatt_toggle_button.click()
        assert calls == ["start", "retry", "stop"]
        assert not win._gatt_timer.isActive()
        assert not win.gatt_retry_button.isEnabled()
        assert win.gatt_toggle_button.text() == "Ligar GATT"
    finally:
        win.close()
        app.processEvents()


def test_gui_close_stops_active_gatt_without_stopping_independent_transport(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(EasyChangeGattClient, "start", lambda self: calls.append("start"))
    monkeypatch.setattr(EasyChangeGattClient, "stop",
                        lambda self, timeout=3.0: calls.append("stop") or True)
    app = _app()
    win = MainWindow(Workspace.open(tmp_path), machine_mode=False)
    win.gatt_toggle_button.click()
    assert calls == ["start"]
    win.close()
    app.processEvents()
    assert "stop" in calls
    assert not win._gatt_timer.isActive()
    assert win.service.gatt_diagnostics_source is None


def test_worker_stop_cancels_async_task_joins_and_clears_sensitive_state(monkeypatch):
    client = EasyChangeGattClient()
    entered = threading.Event()
    exited = threading.Event()

    async def fake_run():
        entered.set()
        try:
            await asyncio.sleep(60)
        finally:
            exited.set()

    monkeypatch.setattr(client, "_run", fake_run)
    client.start()
    assert entered.wait(3)
    client.events.put_nowait({"type": "status", "message": "stale"})
    client.commands.put_nowait(("Q123", "command"))
    assert client.stop(timeout=3.0)
    assert exited.is_set()
    assert client._thread is None
    assert client._loop is None
    assert client._run_task is None
    assert client._session is None
    assert client.commands.empty() and client.events.empty()
    assert client.status()["running"] is False
    assert client.stop(timeout=0)
