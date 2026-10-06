from __future__ import annotations

import os

import pytest

from easychange.core.command_service import CommandService
from easychange.core.human_text_service import HumanTextService
from easychange.core.workspace import Workspace


def test_human_text_persists_outside_workspace_and_pages_utf8(tmp_path, monkeypatch):
    localapp = tmp_path / "localapp"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(localapp))

    service = CommandService(Workspace.open(workspace_dir))
    try:
        text = ("linha αβγ 🚀 " + ("x" * 80) + "\n") * 80
        meta = service.human_text.save(text)

        assert meta["bytes"] == len(text.encode("utf-8"))
        assert meta["lines"] == 80
        assert workspace_dir not in service.human_text.path.parents
        assert str(service.human_text.path).startswith(str(localapp))
        assert not (workspace_dir / ".easychange" / "human-text").exists()

        cursor = 0
        chunks = []
        pages = 0
        while True:
            page = service.human_text.read(cursor=cursor, max_bytes=512)
            chunks.append(page["text"])
            pages += 1
            if page["complete"]:
                assert page["next_cursor"] is None
                break
            cursor = page["next_cursor"]
            assert isinstance(cursor, int)
            assert pages < 100

        assert "".join(chunks) == text
        assert pages > 1
    finally:
        service.close()


def test_human_text_structured_read_and_info_are_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    service = CommandService(Workspace.open(workspace_dir))
    try:
        service.human_text.save("A" * 3000 + " fim")
        info = service._execute_structured({
            "op": "operation",
            "type": "human_text_info",
        })
        assert info.ok is True
        info_data = info.data["results"][0]["data"]
        assert info_data["bytes"] == 3004
        assert info_data["sha256"]

        first = service._execute_structured({
            "op": "operation",
            "type": "human_text_read",
            "cursor": 0,
            "max_bytes": 1024,
        })
        assert first.ok is True
        data = first.data["results"][0]["data"]
        assert data["returned_bytes"] == 1024
        assert data["complete"] is False
        assert data["next_cursor"] == 1024

        second = service._execute_structured({
            "op": "operation",
            "type": "human_text_read",
            "cursor": data["next_cursor"],
            "max_bytes": 2048,
        })
        assert second.ok is True
        assert second.data["results"][0]["data"]["text"].endswith(" fim")

        caps = service.execute(":capabilities").data["capabilities"]["ai_machine"]["structured_operations"]
        assert caps["operations"]["human_text_read"]["family"] == "read"
        assert caps["operations"]["human_text_read"]["continue_on_error_safe"] is True
        assert caps["operations"]["human_text_info"]["mutation"] is False
    finally:
        service.close()


def test_easychange_full_cleanup_removes_human_text_external_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    service = CommandService(Workspace.open(workspace_dir))
    service.human_text.save("texto que deve ser removido")
    stored_path = service.human_text.path
    assert stored_path.exists()

    service.close()
    report = service.processes.purge_durable_state()
    assert report["ok"] is True
    assert not stored_path.exists()


def test_human_mode_large_text_editor_saves_reloads_and_hides_in_machine_mode(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    pytest.importorskip("qrcode")
    from PySide6.QtWidgets import QApplication
    from easychange.ui.main_window import MainWindow

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    app = QApplication.instance() or QApplication([])

    window = MainWindow(Workspace.open(workspace_dir), machine_mode=False)
    window.show()
    app.processEvents()
    try:
        assert window.human_text_panel.isVisible()
        assert window.human_text_editor.isVisible()
        text = ("texto grande para MCP 🚀\n" * 2000)
        window.human_text_editor.setPlainText(text)
        assert window._human_text_dirty is True
        window.save_human_text()
        assert window._human_text_dirty is False
        assert "KiB" in window.human_text_status.text()

        window.toggle_machine()
        app.processEvents()
        assert not window.human_text_panel.isVisible()

        window.toggle_machine()
        app.processEvents()
        assert window.human_text_panel.isVisible()
    finally:
        window.close()
        app.processEvents()

    reopened = MainWindow(Workspace.open(workspace_dir), machine_mode=False)
    reopened.show()
    app.processEvents()
    try:
        assert reopened.human_text_editor.toPlainText() == text
    finally:
        reopened.close()
        app.processEvents()
