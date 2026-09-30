from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QColor, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPushButton,
                               QPlainTextEdit, QSplitter, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace
from easychange.ui.editor import BasicHighlighter, CodeEditor


class MainWindow(QMainWindow):
    def __init__(self, workspace: Workspace, *, machine_mode: bool | None = None, hid_mode: bool = False) -> None:
        super().__init__()
        self.service = CommandService(workspace)
        self._machine = self.service.machine if machine_mode is None else machine_mode
        self.service.machine = self._machine
        self._workspace_dialog_open = False
        if hid_mode:
            self.service.output = "compact"
            self.service.state_store.data["transport"] = "hid"
            self.service.state_store.data["output_mode"] = "compact"
            self.service._persist_state()
        self.setWindowTitle(f"EasyChange — {workspace.name}")
        self.resize(1100, 720)
        central = QWidget(); layout = QVBoxLayout(central)
        self.header = QLabel(f"EasyChange | {workspace.name} | {workspace.kind} | FOCUS: COMMAND")
        self.select_workspace_button = QPushButton("Selecionar projeto…")
        self.select_workspace_button.setObjectName("selectWorkspaceButton")
        self.select_workspace_button.clicked.connect(self.select_workspace)
        header_row = QHBoxLayout(); header_row.addWidget(self.header, 1); header_row.addWidget(self.select_workspace_button)
        self.tree = QTreeWidget(); self.tree.setHeaderLabel("FILES")
        for item in self.service.files.list_files():
            QTreeWidgetItem(self.tree, [item["path"]])
        self.editor = CodeEditor(); self.editor.setPlaceholderText("Open a file with :open <path> or select it in FILES")
        self.output = QPlainTextEdit(); self.output.setReadOnly(True); self.output.setMaximumHeight(180)
        self.output.setObjectName("machineResultRegion")
        self.machine_qr = QLabel(); self.machine_qr.setObjectName("machineResultQr")
        self.machine_qr.setFixedSize(160, 160); self.machine_qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result_panel = QWidget(); result_layout = QHBoxLayout(self.result_panel)
        result_layout.setContentsMargins(0, 0, 0, 0); result_layout.addWidget(self.output, 1); result_layout.addWidget(self.machine_qr)
        self.machine_qr.hide()
        split = QSplitter(Qt.Orientation.Horizontal); split.addWidget(self.tree); split.addWidget(self.editor)
        split.setStretchFactor(1, 1)
        self.command = QLineEdit(); self.command.setPlaceholderText("COMMAND > :remote-guide | Ctrl+K | Enter")
        self.command.setMaxLength(65535)
        self.command.installEventFilter(self)
        layout.addLayout(header_row); layout.addWidget(split, 1); layout.addWidget(self.result_panel); layout.addWidget(self.command)
        self.setCentralWidget(central)
        self.command.returnPressed.connect(self.execute_command)
        self.tree.itemActivated.connect(self.open_selected)
        self.tree.itemDoubleClicked.connect(self.open_selected)
        self.tree.currentItemChanged.connect(self.preview_selected)
        self._active_path: str | None = None
        self._add_shortcut("Ctrl+K", self.focus_command)
        self._add_shortcut("F12", self.toggle_machine)
        self._add_shortcut("Escape", self.focus_command)
        self._add_shortcut("Ctrl+S", self.save_editor)
        self._add_shortcut("Ctrl+P", lambda: self._prompt(":open "))
        self._add_shortcut("Ctrl+Shift+F", lambda: self._prompt(":search "))
        self._add_shortcut("Ctrl+G", lambda: self._prompt(":goto "))
        self._add_shortcut("Ctrl+N", lambda: self._prompt(":new "))
        self._add_shortcut("Ctrl+Shift+S", self.save_all)
        self._add_shortcut("Ctrl+D", lambda: self._run_command(":diff"))
        self._add_shortcut("Ctrl+Z", lambda: self._run_command(":undo"))
        self._add_shortcut("Ctrl+Y", lambda: self._run_command(":redo"))
        self._add_shortcut("F5", lambda: self._run_command(":build"))
        self._add_shortcut("F6", lambda: self._run_command(":test"))
        self._add_shortcut("F7", lambda: self._run_command(":errors"))
        self._add_shortcut("F8", lambda: self._run_command(":next-error"))
        self._add_shortcut("Shift+F8", lambda: self._run_command(":previous-error"))
        if self._machine:
            self._apply_machine_style()
        self._refresh_header()
        self._run_command(":remote")

    def _add_shortcut(self, sequence: str, callback) -> None:
        shortcut = QShortcut(QKeySequence(sequence), self); shortcut.activated.connect(callback)

    def focus_command(self) -> None:
        self.command.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.command and event.type() == QEvent.Type.FocusOut and self._machine and not self._workspace_dialog_open:
            QTimer.singleShot(0, self._restore_command_focus)
        return super().eventFilter(watched, event)

    def _restore_command_focus(self) -> None:
        if not self._workspace_dialog_open:
            self.focus_command()

    def select_workspace(self) -> None:
        self._workspace_dialog_open = True
        try:
            selected = QFileDialog.getExistingDirectory(
                self, "Selecione a pasta do projeto", str(self.service.workspace.root_path),
                QFileDialog.Option.ShowDirsOnly,
            )
            if not selected:
                return
            selected_path = Path(selected).resolve()
            if selected_path == Path(self.service.workspace.root_path).resolve():
                return
            if self.editor.document().isModified() and self._active_path:
                answer = QMessageBox.question(
                    self, "Alterações não salvas",
                    "Salvar as alterações do arquivo aberto antes de trocar de projeto?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes,
                )
                if answer == QMessageBox.StandardButton.Cancel:
                    return
                if answer == QMessageBox.StandardButton.Yes:
                    self.save_editor()
                    if self.editor.document().isModified():
                        return

            new_workspace = Workspace.open(selected_path)
            old_service = self.service
            new_service = CommandService(new_workspace)
            new_service.machine = self._machine
            new_service.output = old_service.output
            new_service.state_store.data["machine_mode"] = self._machine
            new_service.state_store.data["output_mode"] = new_service.output
            new_service._persist_state()
            self.service = new_service
            old_service.close()

            self._active_path = None
            self.editor.clear()
            self.editor.document().setModified(False)
            self.tree.clear()
            for item in self.service.files.list_files():
                QTreeWidgetItem(self.tree, [item["path"]])
            self.setWindowTitle(f"EasyChange — {new_workspace.name}")
            self._refresh_header()
            self._run_command(":remote")
        except (OSError, ValueError, PermissionError) as exc:
            QMessageBox.critical(self, "Não foi possível abrir o projeto", str(exc))
        finally:
            self._workspace_dialog_open = False
            self.focus_command()

    def toggle_machine(self) -> None:
        self._machine = not self._machine
        self.service.machine = self._machine
        self.service._persist_state()
        self._apply_machine_style()
        self._refresh_header()
        self.focus_command()

    def _apply_machine_style(self) -> None:
        self.setStyleSheet("QWidget { background:#101820; color:#f2f5f7; font-family: Consolas, monospace; }" if self._machine else "")
        self.machine_qr.setVisible(self._machine and bool(self.machine_qr.pixmap()))
        self.editor.highlighter.setDocument(None)
        self.editor.highlighter = BasicHighlighter(self.editor.document(), high_contrast=self._machine)

    def _refresh_header(self) -> None:
        transport = "HID/COMPACT" if self.service.output == "compact" else "TEXT"
        self.header.setText(f"EasyChange | {'MACHINE' if self._machine else 'HUMAN'} | {transport} | {self.service.workspace.root_path} | FOCUS: COMMAND")

    def execute_command(self) -> None:
        text = self.command.text(); self.command.clear()
        self._run_command(text)

    def _run_command(self, text: str) -> None:
        result = self.service.execute(text)
        if self._machine != self.service.machine:
            self._machine = self.service.machine
            self._apply_machine_style()
        self._refresh_header()
        self.output.setPlainText(result.render(self.service.output))
        self._update_machine_qr(result)
        if result.ok and (result.data.get("path") or result.data.get("file")) and text.lstrip(":").split(maxsplit=1)[0] in {"open", "read", "context", "goto", "reload"}:
            try:
                data = result.data
                if data.get("file") and not data.get("path"):
                    data = self.service.files.read(data["file"], data.get("line", 1), 1)
                self._load_editor(data)
                if result.data.get("start"):
                    self.editor.goto_line(result.data["start"])
            except (OSError, ValueError, PermissionError) as exc:
                self.output.setPlainText(f"ERROR: {exc}")
        self.focus_command()

    def _update_machine_qr(self, result) -> None:
        self.machine_qr.clear()
        self.machine_qr.setVisible(False)
        if not self._machine:
            return
        try:
            import json
            import qrcode
            compact_data = {}
            if isinstance(result.data, dict):
                allowed = {"state", "workspace", "workspace_id", "instance_id", "name", "type", "file", "line", "dirty",
                           "transaction", "git", "count", "matches", "path", "change_id", "duplicate"}
                compact_data = {key: value for key, value in result.data.items() if key in allowed}
            packet = {
                "protocol": "EC1", "sequence": result.sequence,
                "status": "OK" if result.ok else "ERR", "command": result.command,
                "command_id": result.command_id, "duration_ms": result.duration_ms,
                "code": result.code, "error": result.error, "data": compact_data,
            }
            payload = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
            if len(payload.encode("utf-8")) > 700:
                return
            code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=1, border=3)
            code.add_data(payload); code.make(fit=True)
            matrix = code.get_matrix(); height = len(matrix); width = len(matrix[0])
            image = QImage(width, height, QImage.Format.Format_RGB32)
            for y, row in enumerate(matrix):
                for x, dark in enumerate(row):
                    image.setPixelColor(x, y, QColor(0, 0, 0) if dark else QColor(255, 255, 255))
            pixmap = QPixmap.fromImage(image).scaled(self.machine_qr.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                     Qt.TransformationMode.FastTransformation)
            self.machine_qr.setPixmap(pixmap); self.machine_qr.setVisible(True)
        except (ImportError, ValueError, RuntimeError):
            # Plain compact text remains the complete fallback protocol.
            return

    def _prompt(self, value: str) -> None:
        self.command.setText(value); self.focus_command(); self.command.setCursorPosition(len(value))

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
        self.service.files.remember(path, replace=True)
        self._active_path = path
        self.editor.setPlainText(target.read_text(encoding="utf-8-sig"))
        self.editor.document().setModified(False)

    def save_editor(self) -> None:
        if self._active_path:
            result = self.service.execute_tokens("write", [self._active_path, self.editor.toPlainText()], raw=f"GUI save {self._active_path}")
            self.output.setPlainText(result.render(self.service.output))
            if result.ok:
                self.editor.document().setModified(False)
        self.focus_command()

    def save_all(self) -> None:
        self.save_editor()
        self.output.appendPlainText("SAVEALL: 1 active file")

    def closeEvent(self, event) -> None:
        self.service.close()
        super().closeEvent(event)

    @staticmethod
    def _quote(value: str) -> str:
        return '"' + value.replace('"', '\\"') + '"'


def run(workspace_path: str = ".", *, machine_mode: bool | None = None, hid_mode: bool = False) -> int:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(Path(workspace_path)), machine_mode=machine_mode, hid_mode=hid_mode)
    window.show()
    if window._machine:
        QTimer.singleShot(0, window.focus_command)
    return app.exec()
