from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QPlainTextEdit, QSplitter, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


class MainWindow(QMainWindow):
    def __init__(self, workspace: Workspace) -> None:
        super().__init__()
        self.service = CommandService(workspace)
        self.setWindowTitle("EasyChange")
        self.resize(1100, 720)
        central = QWidget(); layout = QVBoxLayout(central)
        self.header = QLabel(f"EasyChange | {workspace.name} | {workspace.kind}")
        self.tree = QTreeWidget(); self.tree.setHeaderLabel("FILES")
        for item in self.service.files.list_files():
            QTreeWidgetItem(self.tree, [item["path"]])
        self.editor = QPlainTextEdit(); self.editor.setPlaceholderText("Open a file with :open <path> or select it in FILES")
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.output = QPlainTextEdit(); self.output.setReadOnly(True); self.output.setMaximumHeight(180)
        split = QSplitter(Qt.Orientation.Horizontal); split.addWidget(self.tree); split.addWidget(self.editor)
        split.setStretchFactor(1, 1)
        self.command = QLineEdit(); self.command.setPlaceholderText("COMMAND > :help")
        layout.addWidget(self.header); layout.addWidget(split, 1); layout.addWidget(self.output); layout.addWidget(self.command)
        self.setCentralWidget(central)
        self.command.returnPressed.connect(self.execute_command)
        self.tree.itemActivated.connect(self.open_selected)
        self.tree.itemDoubleClicked.connect(self.open_selected)
        self.tree.currentItemChanged.connect(self.preview_selected)
        self._machine = False
        self._active_path: str | None = None
        self._add_shortcut("Ctrl+K", self.focus_command)
        self._add_shortcut("F12", self.toggle_machine)
        self._add_shortcut("Escape", self.focus_command)
        self._add_shortcut("Ctrl+S", self.save_editor)
        self.focus_command()

    def _add_shortcut(self, sequence: str, callback) -> None:
        shortcut = QShortcut(QKeySequence(sequence), self); shortcut.activated.connect(callback)

    def focus_command(self) -> None:
        self.command.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def toggle_machine(self) -> None:
        self._machine = not self._machine
        self.service.machine = self._machine
        self.setStyleSheet("QWidget { background:#101820; color:#f2f5f7; font-family: Consolas, monospace; }" if self._machine else "")
        self.header.setText(f"EasyChange {'MACHINE MODE' if self._machine else 'HUMAN MODE'} | {self.service.workspace.root_path}")
        self.focus_command()

    def execute_command(self) -> None:
        text = self.command.text(); self.command.clear()
        result = self.service.execute(text)
        self.output.setPlainText(result.render(self.service.output))
        if result.data.get("path") and text.lstrip(":").startswith(("open", "read")):
            self._load_editor(result.data)
        self.focus_command()

    def preview_selected(self, current, previous=None) -> None:
        if current:
            try: self._load_editor(self.service.files.read(current.text(0)))
            except (OSError, ValueError): pass

    def open_selected(self, item, column=0) -> None:
        self.preview_selected(item)
        self.focus_command()

    def _load_editor(self, data: dict) -> None:
        path = data.get("path")
        if not path:
            return
        target = self.service.workspace.resolve(path, must_exist=True)
        if target.stat().st_size > 2 * 1024 * 1024:
            self._active_path = None
            self.editor.clear()
            self.output.setPlainText("FILE_TOO_LARGE: editor limit is 2 MiB; use :read with a line range")
            return
        self._active_path = path
        self.editor.setPlainText(target.read_text(encoding="utf-8-sig"))

    def save_editor(self) -> None:
        if self._active_path:
            result = self.service.execute(":write " + self._quote(self._active_path) + " " + self._quote(self.editor.toPlainText()))
            self.output.setPlainText(result.render(self.service.output))
        self.focus_command()

    @staticmethod
    def _quote(value: str) -> str:
        return '"' + value.replace('"', '\\"') + '"'


def run(workspace_path: str = ".") -> int:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(Path(workspace_path)))
    window.show()
    return app.exec()
