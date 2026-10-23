"""GUI edit baseline must refer to the bytes shown, not a second disk read."""
import hashlib
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtWidgets import QApplication

from easychange.core.file_service import ExternalChangeError
from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow


def test_concurrent_edit_during_preview_never_authorizes_stale_save(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "code.cs"
    path.write_bytes(b"original content")
    window = MainWindow(Workspace.open(tmp_path), machine_mode=False)
    remember = window.service.files.remember

    def concurrent_edit_before_remember(name, **kwargs):
        # This happens AFTER the GUI reads the old bytes.
        path.write_bytes(b"external newer content")
        return remember(name, **kwargs)

    monkeypatch.setattr(window.service.files, "remember", concurrent_edit_before_remember)
    try:
        window.preview_selected(SimpleNamespace(text=lambda _col: "code.cs"))
        assert window.editor.toPlainText() == "original content"
        assert window.service.files.baselines["code.cs"] == hashlib.sha256(
            b"original content").hexdigest()
        with pytest.raises(ExternalChangeError):
            window.service.files.write("code.cs", "stale editor save")
        assert path.read_bytes() == b"external newer content"
    finally:
        window.close()
        app.processEvents()


def test_gui_preview_skips_second_file_hash(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    (tmp_path / "code.cs").write_bytes(b"snapshot")
    window = MainWindow(Workspace.open(tmp_path), machine_mode=False)
    monkeypatch.setattr(
        window.service.files, "_hash_file_stream",
        lambda *_args, **_kwargs: pytest.fail("UNNECESSARY_SECOND_FILE_READ"),
    )
    try:
        window._load_editor({"path": "code.cs"})
        assert window.editor.toPlainText() == "snapshot"
        assert window.service.files.baselines["code.cs"] == hashlib.sha256(
            b"snapshot").hexdigest()
    finally:
        window.close()
        app.processEvents()
