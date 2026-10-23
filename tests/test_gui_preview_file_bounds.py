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




def test_external_change_after_gui_preview_cannot_be_overwritten(tmp_path):
    from types import SimpleNamespace
    from easychange.core.file_service import ExternalChangeError

    app = QApplication.instance() or QApplication([])
    document = tmp_path / "guard.txt"
    document.write_text("open version", encoding="utf-8")
    window = MainWindow(Workspace.open(tmp_path), machine_mode=False)
    try:
        window.preview_selected(SimpleNamespace(text=lambda _column: "guard.txt"))
        assert window._active_path == "guard.txt"
        assert window.editor.toPlainText() == "open version"

        document.write_text("external edit", encoding="utf-8")
        with pytest.raises(ExternalChangeError):
            window.service.files.write("guard.txt", "outdated GUI save")
        assert document.read_text(encoding="utf-8") == "external edit"
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("invalid", [b"binary\x00data", b"invalid\xffutf8"])
def test_invalid_preview_keeps_existing_editor_and_baseline(tmp_path, invalid):
    from types import SimpleNamespace

    app = QApplication.instance() or QApplication([])
    (tmp_path / "valid.txt").write_text("keep this", encoding="utf-8")
    (tmp_path / "invalid.bin").write_bytes(invalid)
    window = MainWindow(Workspace.open(tmp_path), machine_mode=False)
    try:
        window.preview_selected(SimpleNamespace(text=lambda _column: "valid.txt"))
        baseline = window.service.files._baselines["valid.txt"]
        window.preview_selected(SimpleNamespace(text=lambda _column: "invalid.bin"))
        assert window._active_path == "valid.txt"
        assert window.editor.toPlainText() == "keep this"
        assert window.service.files._baselines["valid.txt"] == baseline
    finally:
        window.close()
        app.processEvents()
