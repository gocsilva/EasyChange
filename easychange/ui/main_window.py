from __future__ import annotations
from easychange.remote.optical_protocol import encode_result_chunks, encode_result_header

from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QColor, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPushButton,
                               QPlainTextEdit, QSplitter, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from easychange.core.command_service import CommandService
from easychange.core.maintenance_service import MaintenanceService
from easychange.core.workspace import Workspace
from easychange.ui.editor import BasicHighlighter, CodeEditor


class MainWindow(QMainWindow):
    # Small results keep the editor visible. Large results temporarily turn
    # the HDMI surface into a reliable 4x4 optical modem. Physical HDMI testing
    # showed 16 larger cells outperforms 32 dense cells because decode success
    # matters more than theoretical QR count.
    _OPTICAL_NORMAL_SLOTS = 4
    _OPTICAL_BURST_SLOTS = 16
    _OPTICAL_BURST_COLUMNS = 4
    # More than one 4-QR row should not rotate in compact mode.
    # Switch to the 16-slot canvas so 5-16 chunks are visible simultaneously
    # and duplicated padding adds optical redundancy.
    _OPTICAL_BURST_THRESHOLD = 4

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
        self.resize(1400, 820)
        central = QWidget(); layout = QVBoxLayout(central)
        self.header = QLabel(f"EasyChange | {workspace.name} | {workspace.kind} | FOCUS: COMMAND")
        self.select_workspace_button = QPushButton("Selecionar projeto…")
        self.select_workspace_button.setObjectName("selectWorkspaceButton")
        self.select_workspace_button.clicked.connect(self.select_workspace)
        self.cleanup_easychange_button = QPushButton("Limpar dados do EasyChange")
        self.cleanup_easychange_button.setObjectName("cleanupEasyChangeButton")
        self.cleanup_easychange_button.setToolTip(
            "Remove somente dados, caches, índices, histórico e temporários criados pelo EasyChange neste projeto."
        )
        self.cleanup_easychange_button.clicked.connect(self.cleanup_easychange_data)
        header_row = QHBoxLayout()
        header_row.addWidget(self.header, 1)
        header_row.addWidget(self.select_workspace_button)
        header_row.addWidget(self.cleanup_easychange_button)

        self.tree = QTreeWidget(); self.tree.setHeaderLabel("FILES")
        if not self._machine:
            self._populate_tree()
        self.editor = CodeEditor(); self.editor.setPlaceholderText("Open a file with :open <path> or select it in FILES")

        self.output = QPlainTextEdit(); self.output.setReadOnly(True)
        self.output.setMaximumHeight(330 if self._machine else 190)
        self.output.setObjectName("machineResultRegion")
        self.machine_qrs: list[QLabel] = []
        for index in range(self._OPTICAL_BURST_SLOTS):
            label = QLabel()
            label.setObjectName("machineResultQr" if index == 0 else f"machineResultQr{index + 1}")
            label.setFixedSize(160, 160)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.hide()
            self.machine_qrs.append(label)
        self.machine_qr = self.machine_qrs[0]

        self._optical_payloads: list[str] = []
        self._optical_header_payload: str | None = None
        self._optical_pixmaps: dict[str, QPixmap] = {}
        self._optical_page = 0
        self._optical_burst = False
        self._optical_timer = QTimer(self)
        self._optical_timer.setInterval(420)
        self._optical_timer.timeout.connect(self._render_optical_page)

        self.result_panel = QWidget(); result_layout = QHBoxLayout(self.result_panel)
        result_layout.setContentsMargins(0, 0, 0, 0)
        result_layout.setSpacing(4)
        result_layout.addWidget(self.output, 1)
        self.qr_panel = QWidget()
        qr_layout = QGridLayout(self.qr_panel)
        qr_layout.setContentsMargins(0, 0, 0, 0)
        qr_layout.setHorizontalSpacing(4)
        qr_layout.setVerticalSpacing(4)
        for index, label in enumerate(self.machine_qrs):
            qr_layout.addWidget(label, index // self._OPTICAL_BURST_COLUMNS, index % self._OPTICAL_BURST_COLUMNS)
        result_layout.addWidget(self.qr_panel, 0, Qt.AlignmentFlag.AlignCenter)

        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(self.tree)
        self.split.addWidget(self.editor)
        self.split.setStretchFactor(1, 1)

        self.command = QLineEdit(); self.command.setPlaceholderText("COMMAND > :remote-guide | Ctrl+K | Enter")
        self.command.setMaxLength(65535)
        self.command.installEventFilter(self)
        layout.addLayout(header_row); layout.addWidget(self.split, 1); layout.addWidget(self.result_panel); layout.addWidget(self.command)
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
            if not self._machine:
                self._populate_tree()
            self.setWindowTitle(f"EasyChange — {new_workspace.name}")
            self._refresh_header()
            self._run_command(":remote")
        except (OSError, ValueError, PermissionError) as exc:
            QMessageBox.critical(self, "Não foi possível abrir o projeto", str(exc))
        finally:
            self._workspace_dialog_open = False
            self.focus_command()

    def cleanup_easychange_data(self) -> None:
        """Fully remove EasyChange-owned state from the open workspace."""
        if self._machine:
            return
        workspace_path = Path(self.service.workspace.root_path).resolve()
        answer = QMessageBox.question(
            self,
            "Limpar dados do EasyChange",
            (
                "Isso removerá somente os dados criados pelo EasyChange neste projeto:\n\n"
                f"{workspace_path}\n\n"
                "Serão removidos histórico/undo, índice, jobs, resultados, evidências, caches, "
                "configurações internas do EasyChange e temporários próprios.\n\n"
                "O código-fonte, a pasta .git e outros arquivos do projeto NÃO serão apagados.\n\n"
                "Deseja continuar?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        active_path = self._active_path
        editor_text = self.editor.toPlainText()
        editor_modified = self.editor.document().isModified()
        previous_output = self.service.output
        old_service = self.service
        report = None
        try:
            old_service.close()
            report = MaintenanceService(workspace_path).purge_all()
            new_service = CommandService(Workspace.open(workspace_path))
            new_service.machine = False
            new_service.output = previous_output
            new_service.state_store.data["mode"] = "human"
            new_service.state_store.data["output_mode"] = previous_output
            new_service._persist_state()
            self.service = new_service
            self._machine = False

            if active_path:
                try:
                    target = self.service.workspace.resolve(active_path, must_exist=True)
                    if target.is_file():
                        self.service.files.remember(active_path, replace=True)
                        self._active_path = active_path
                    else:
                        self._active_path = None
                except (OSError, ValueError, PermissionError):
                    self._active_path = None

            if editor_modified and self._active_path:
                self.editor.setPlainText(editor_text)
                self.editor.document().setModified(True)

            self.tree.clear()
            self._populate_tree()
            self._apply_machine_style()
            self._refresh_header()

            reclaimed = int((report or {}).get("reclaimed_bytes") or 0)
            removed = int((report or {}).get("removed_files") or 0)
            self.output.setPlainText(
                f"EasyChange limpo. Itens removidos: {removed} | "
                f"Espaço recuperado: {reclaimed / (1024 * 1024):.2f} MiB"
            )
            QMessageBox.information(
                self,
                "Limpeza concluída",
                (
                    "Os dados do EasyChange foram removidos do projeto.\n\n"
                    f"Itens removidos: {removed}\n"
                    f"Espaço recuperado: {reclaimed / (1024 * 1024):.2f} MiB\n\n"
                    "O projeto e o código-fonte foram preservados."
                ),
            )
        except Exception as exc:
            # Always restore a usable service even if cleanup/reinitialization failed.
            try:
                self.service = CommandService(Workspace.open(workspace_path))
                self.service.machine = False
                self._machine = False
                self._apply_machine_style()
                self._refresh_header()
            except Exception:
                pass
            QMessageBox.critical(self, "Falha na limpeza do EasyChange", str(exc))
        finally:
            self.focus_command()

    def _populate_tree(self) -> None:
        self.tree.clear()
        for item in self.service.files.list_files():
            QTreeWidgetItem(self.tree, [item["path"]])
    def toggle_machine(self) -> None:
        self._machine = not self._machine
        self.service.machine = self._machine
        self.service._persist_state()
        if not self._machine and self.tree.topLevelItemCount() == 0:
            self._populate_tree()
        self._apply_machine_style()
        self._refresh_header()
        self.focus_command()

    def _apply_machine_style(self) -> None:
        self.setStyleSheet("QWidget { background:#101820; color:#f2f5f7; font-family: Consolas, monospace; }" if self._machine else "")
        self.tree.setVisible(not self._machine)
        self.select_workspace_button.setVisible(not self._machine)
        self.cleanup_easychange_button.setVisible(not self._machine)
        self.output.setMaximumHeight(330 if self._machine else 190)
        if not self._machine:
            self._set_optical_burst(False)
        elif self._optical_burst:
            self.split.setVisible(False)
            self.output.setVisible(False)
        for label in self.machine_qrs:
            label.setVisible(self._machine and bool(label.pixmap()))
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

    def _set_optical_burst(self, enabled: bool) -> None:
        enabled = bool(enabled and self._machine)
        self._optical_burst = enabled
        if enabled:
            self.split.setVisible(False)
            self.output.setVisible(False)
            self.result_panel.setMinimumHeight(660)
            self.result_panel.setMaximumHeight(16777215)
            # Capture p95 is ~200ms on the physical HDMI path; keep a
            # page visible long enough to guarantee at least one fresh frame.
            self._optical_timer.setInterval(320)
        else:
            self.split.setVisible(True)
            self.output.setVisible(True)
            self.result_panel.setMinimumHeight(0)
            self.result_panel.setMaximumHeight(340 if self._machine else 200)
            # Normal multi-page mode is only used for <=4 visible slots;
            # leave each page up long enough for HDMI capture + QR decode.
            self._optical_timer.setInterval(420)
    def _update_machine_qr(self, result) -> None:
        self._optical_timer.stop()
        self._optical_payloads = []
        self._optical_header_payload = None
        self._optical_pixmaps.clear()
        self._optical_page = 0
        for label in self.machine_qrs:
            label.clear()
            label.setVisible(False)
        if not self._machine:
            self._set_optical_burst(False)
            return
        try:
            self._optical_payloads = encode_result_chunks(result)
            self._set_optical_burst(len(self._optical_payloads) > self._OPTICAL_BURST_THRESHOLD)
            if self._optical_burst:
                self._optical_header_payload = encode_result_header(
                    result, chunk_count=len(self._optical_payloads)
                )
            self._render_optical_page()
            slots = self._OPTICAL_BURST_SLOTS if self._optical_burst else self._OPTICAL_NORMAL_SLOTS
            data_slots = max(1, slots - (1 if self._optical_header_payload else 0))
            if len(self._optical_payloads) > data_slots:
                self._optical_timer.start()
        except (ImportError, ValueError, RuntimeError):
            self._set_optical_burst(False)
            # Compact EC1 text remains a correlation fallback.
            return

    def _render_optical_page(self) -> None:
        if not self._machine or not self._optical_payloads:
            return
        import qrcode

        requested_slots = self._OPTICAL_BURST_SLOTS if self._optical_burst else self._OPTICAL_NORMAL_SLOTS
        slots = min(requested_slots, len(self.machine_qrs))
        header_slots = 1 if self._optical_header_payload else 0
        data_slots = max(1, slots - header_slots)
        page_count = max(1, (len(self._optical_payloads) + data_slots - 1) // data_slots)
        page = self._optical_page % page_count
        start = page * data_slots
        payloads = self._optical_payloads[start:start + data_slots]
        if self._optical_header_payload:
            payloads = [self._optical_header_payload, *payloads]
        # In burst mode keep all optical cells populated even on the final
        # partial page. Repeating already-present chunks costs no protocol state
        # and lets the HDMI decoder reliably identify the dense burst grid.
        if self._optical_burst and payloads and len(payloads) < slots:
            original = list(payloads)
            index = 0
            while len(payloads) < slots:
                payloads.append(original[index % len(original)])
                index += 1

        for label in self.machine_qrs:
            label.clear()
            label.setVisible(False)

        for label, payload in zip(self.machine_qrs[:slots], payloads):
            pixmap = self._optical_pixmaps.get(payload)
            if pixmap is None:
                # Fixed mask avoids qrcode's expensive 8-mask visual scoring.
                # EC2 is machine-readable, so a valid deterministic mask is
                # preferable to spending CPU choosing the prettiest one.
                code = qrcode.QRCode(
                    error_correction=qrcode.constants.ERROR_CORRECT_L,
                    box_size=1,
                    border=2,
                    mask_pattern=3,
                )
                code.add_data(payload)
                code.make(fit=True)
                matrix = code.get_matrix()
                height = len(matrix); width = len(matrix[0])
                pixels = bytes(0 if dark else 255 for row in matrix for dark in row)
                image = QImage(
                    pixels, width, height, width, QImage.Format.Format_Grayscale8
                ).copy()
                scale = max(1, min(label.width() // width, label.height() // height))
                render_width = width * scale
                render_height = height * scale
                pixmap = QPixmap.fromImage(image).scaled(
                    render_width,
                    render_height,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.FastTransformation,
                )
                self._optical_pixmaps[payload] = pixmap
            label.setPixmap(pixmap)
            label.setVisible(True)

        self._optical_page = (page + 1) % page_count

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
