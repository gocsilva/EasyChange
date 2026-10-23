"""GUI preview must not load/hash files before checking its editor limit."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow


def test_oversized_preview_skips_expensive_file_service_read(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    huge = tmp_path / "huge.cs"
    huge.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    window = MainWindow(Workspace.open(tmp_path), machine_mode=False)
    read_calls = []

    def unexpected_read(path):
        read_calls.append(path)
        raise AssertionError("Large files must not be fully read for preview")

    monkeypatch.setattr(window.service.files, "read", unexpected_read)
    try:
        item = SimpleNamespace(text=lambda column: "huge.cs")
        window.preview_selected(item)
        assert not read_calls
        assert window._active_path is None
        assert "FILE_TOO_LARGE" in window.output.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_regular_preview_keeps_content_and_edit_baseline(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    (tmp_path / "small.cs").write_text("café\nnamespace App {}", encoding="utf-8")
    window = MainWindow(Workspace.open(tmp_path), machine_mode=False)
    monkeypatch.setattr(
        window.service.files, "read",
        lambda *_args, **_kwargs: pytest.fail("No duplicate FileService.read"),
    )
    try:
        window.preview_selected(SimpleNamespace(text=lambda _: "small.cs"))
        assert window.editor.toPlainText() == "café\nnamespace App {}"
        assert window._active_path == "small.cs"
        assert not window.editor.document().isModified()
        assert "small.cs" in window.service.files._baselines
    finally:
        window.close()
        app.processEvents()


def test_direct_editor_load_still_rejects_binary(tmp_path):
    app = QApplication.instance() or QApplication([])
    (tmp_path / "binary.cs").write_bytes(b"x\x00y")
    window = MainWindow(Workspace.open(tmp_path), machine_mode=False)
    try:
        with pytest.raises(ValueError, match="Binary"):
            window._load_editor({"path": "binary.cs"})
        assert window._active_path is None
    finally:
        window.close()
        app.processEvents()
