from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from easychange.remote.optical_protocol import encode_result_bundle

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QColor, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPushButton, QSpinBox,
                               QPlainTextEdit, QSplitter, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from easychange.core.command_service import CommandService
from easychange.core.maintenance_service import MaintenanceService
from easychange.core.workspace import Workspace
from easychange.ui.editor import BasicHighlighter, CodeEditor


def _direct_recovery_optical_payloads(result) -> list[str] | None:
    """Return original EC2 packets for durable optical recovery queries.

    optical_chunk(s) already carry identity/CRC-bearing EC2 packets from the
    original result. Re-encoding the recovery Result would create a nested EC2
    envelope that can be larger than the payload being recovered.
    """
    if not bool(getattr(result, "ok", False)):
        return None
    command = str(getattr(result, "command", "") or "").casefold()
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return None

    if command == "optical_chunk":
        candidates = [data.get("packet")]
    elif command == "optical_chunks":
        candidates = data.get("packets")
        if not isinstance(candidates, list):
            return None
    else:
        return None

    payloads: list[str] = []
    for packet in candidates:
        if not isinstance(packet, dict) or packet.get("p") != "EC2":
            return None
        if not str(packet.get("s") or ""):
            return None
        try:
            index = int(packet.get("i"))
            total = int(packet.get("n"))
        except (TypeError, ValueError):
            return None
        if index < 0 or total <= 0 or index >= total:
            return None
        payloads.append(
            json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
        )
    return payloads or None


class MainWindow(QMainWindow):
    # Small results keep the editor visible. Large results temporarily turn
    # the HDMI surface into a reliable 4x4 optical modem. Physical HDMI testing
    # showed 16 larger cells outperforms 32 dense cells because decode success
    # matters more than theoretical QR count.
    _OPTICAL_NORMAL_SLOTS = 4
    _OPTICAL_BURST_SLOTS = 16
    _OPTICAL_BURST_COLUMNS = 4
    # A burst page must remain visible for more than two typical HDMI capture
    # periods. This reduces missed pages under paint/capture/decode jitter.
    _OPTICAL_BURST_INTERVAL_MS = 900
    # Bound the initial optical broadcast to one 4x4 page: one EC2H header
    # plus up to 15 data packets. Large results are durable in ResultStore and
    # MCP switches to result_meta/result_chunks after this first page. Rotating
    # a second initial page only added ~1 dwell interval and more QR decode work
    # before the same durable handoff.
    _OPTICAL_BROADCAST_MAX_CHUNKS = 15
    _OPTICAL_OVERFLOW_BROADCAST_CHUNKS = 3
    # More than one 4-QR row should not rotate in compact mode.
    # Switch to the 16-slot canvas so 5-16 chunks are visible simultaneously
    # and duplicated padding adds optical redundancy.
    _OPTICAL_BURST_THRESHOLD = 4

    def __init__(self, workspace: Workspace, *, machine_mode: bool | None = None, hid_mode: bool = False) -> None:
        super().__init__()
        self.service = CommandService(workspace)
        self.service.browser.set_focus_window_handle_provider(lambda: int(self.winId()))
        self._machine = self.service.machine if machine_mode is None else machine_mode
        self.service.machine = self._machine
        self._workspace_dialog_open = False

        # An explicit human launch is a mode override, not merely a visual hint.
        # The persisted session is commonly left in machine/HID/compact mode by
        # the remote launcher. Without resetting that state, the GUI launcher can
        # reopen as Machine Mode or render compact transport output and look
        # "broken" even though the window process started successfully.
        if machine_mode is False and not hid_mode:
            self.service.machine = False
            self.service.output = "text"
            self.service.state_store.data["mode"] = "human"
            self.service.state_store.data["transport"] = "local"
            self.service.state_store.data["output_mode"] = "text"
            self.service._persist_state()

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

        self.optical_controls = QWidget()
        self.optical_controls.setObjectName("opticalTransportControls")
        optical_layout = QHBoxLayout(self.optical_controls)
        optical_layout.setContentsMargins(0, 0, 0, 0)
        optical_layout.setSpacing(6)
        self.optical_status = QLabel()
        self.optical_status.setObjectName("opticalTransportStatus")
        self.optical_status.setToolTip(
            "EC2 usa dwell adaptativo. O MCP deve aumentar o tempo quando HDMI/decoder "
            "estiverem lentos ou houver RESULT_INCOMPLETE e reduzir gradualmente quando "
            "as entregas estiverem saudáveis."
        )
        self.optical_adaptive = QCheckBox("EC2 adaptativo (MCP ajusta)")
        self.optical_adaptive.setObjectName("opticalAdaptiveEnabled")
        self.optical_min = QSpinBox()
        self.optical_min.setObjectName("opticalMinMs")
        self.optical_min.setRange(300, 5000)
        self.optical_min.setPrefix("mín ")
        self.optical_min.setSuffix(" ms")
        self.optical_current = QSpinBox()
        self.optical_current.setObjectName("opticalCurrentMs")
        self.optical_current.setRange(300, 5000)
        self.optical_current.setPrefix("atual ")
        self.optical_current.setSuffix(" ms")
        self.optical_max = QSpinBox()
        self.optical_max.setObjectName("opticalMaxMs")
        self.optical_max.setRange(300, 5000)
        self.optical_max.setPrefix("máx ")
        self.optical_max.setSuffix(" ms")
        optical_layout.addWidget(self.optical_status, 1)
        optical_layout.addWidget(self.optical_adaptive)
        optical_layout.addWidget(self.optical_min)
        optical_layout.addWidget(self.optical_current)
        optical_layout.addWidget(self.optical_max)
        self._sync_optical_transport_controls()
        self.optical_adaptive.toggled.connect(self._save_optical_transport_controls)
        self.optical_min.editingFinished.connect(self._save_optical_transport_controls)
        self.optical_current.editingFinished.connect(self._save_optical_transport_controls)
        self.optical_max.editingFinished.connect(self._save_optical_transport_controls)

        self.tree = QTreeWidget(); self.tree.setHeaderLabel("FILES")
        if not self._machine:
            self._populate_tree()
        self.editor = CodeEditor(); self.editor.setPlaceholderText("Open a file with :open <path> or select it in FILES")

        self.human_text_panel = QWidget()
        self.human_text_panel.setObjectName("humanTextPanel")
        human_text_layout = QVBoxLayout(self.human_text_panel)
        human_text_layout.setContentsMargins(0, 0, 0, 0)
        human_text_header = QHBoxLayout()
        self.human_text_title = QLabel("TEXTO PARA AGENTE / MCP")
        self.human_text_title.setToolTip(
            "Cole aqui textos grandes. Agentes externos podem ler o conteúdo salvo via EasyChange/EC2/QR."
        )
        self.human_text_status = QLabel("Carregando…")
        self.human_text_status.setObjectName("humanTextStatus")
        self.human_text_save_button = QPushButton("Salvar agora")
        self.human_text_save_button.setObjectName("humanTextSaveButton")
        self.human_text_clear_button = QPushButton("Limpar texto")
        self.human_text_clear_button.setObjectName("humanTextClearButton")
        human_text_header.addWidget(self.human_text_title)
        human_text_header.addWidget(self.human_text_status, 1)
        human_text_header.addWidget(self.human_text_save_button)
        human_text_header.addWidget(self.human_text_clear_button)
        self.human_text_editor = QPlainTextEdit()
        self.human_text_editor.setObjectName("humanTextEditor")
        self.human_text_editor.setPlaceholderText(
            "Cole aqui textos grandes para agentes MCP/IA lerem via QR. "
            "O conteúdo é salvo fora do projeto Git e fica associado a este workspace."
        )
        human_text_layout.addLayout(human_text_header)
        human_text_layout.addWidget(self.human_text_editor, 1)
        self._human_text_loading = False
        self._human_text_dirty = False
        self._human_text_save_timer = QTimer(self)
        self._human_text_save_timer.setSingleShot(True)
        self._human_text_save_timer.setInterval(1200)
        self._human_text_save_timer.timeout.connect(self._autosave_human_text)
        self.human_text_editor.textChanged.connect(self._schedule_human_text_save)
        self.human_text_save_button.clicked.connect(self.save_human_text)
        self.human_text_clear_button.clicked.connect(self.clear_human_text)

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
        self._optical_render_generation = 0
        self._optical_render_pending: dict | None = None
        self._optical_encode_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="easychange-ec2",
        )
        self._optical_encode_future = None
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

        self.workspace_split = QSplitter(Qt.Orientation.Vertical)
        self.workspace_split.setObjectName("humanWorkspaceSplit")
        self.workspace_split.addWidget(self.split)
        self.workspace_split.addWidget(self.human_text_panel)
        self.workspace_split.setStretchFactor(0, 3)
        self.workspace_split.setStretchFactor(1, 2)

        self.command = QLineEdit(); self.command.setPlaceholderText("COMMAND > :remote-guide | Ctrl+K | Enter")
        self.command.setMaxLength(65535)
        self.command.installEventFilter(self)
        layout.addLayout(header_row); layout.addWidget(self.optical_controls); layout.addWidget(self.workspace_split, 1); layout.addWidget(self.result_panel); layout.addWidget(self.command)
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
        self._load_human_text()
        self._apply_machine_style()
        self._refresh_header()
        self._run_command(":remote")

    def _add_shortcut(self, sequence: str, callback) -> None:
        shortcut = QShortcut(QKeySequence(sequence), self); shortcut.activated.connect(callback)

    def focus_command(self) -> dict:
        # Keep focus inside COMMAND only while EasyChange is already active.
        # This helper must never promote or unminimize the application.
        state = self.service.browser.easychange_focus_state()
        if self.isMinimized():
            return {
                **state,
                "focus_restore_attempted": False,
                "focus_restore_skipped": "USER_MINIMIZED",
                "user_focus_override": True,
            }
        if not self.isActiveWindow():
            return {
                **state,
                "focus_restore_attempted": False,
                "focus_restore_skipped": "WINDOW_NOT_ACTIVE",
                "user_focus_override": True,
            }
        self.command.setFocus(Qt.FocusReason.ShortcutFocusReason)
        QApplication.processEvents()
        state = self.service.browser.easychange_focus_state()
        return {
            **state,
            "focus_restore_attempted": False,
            "focus_restore_skipped": "UI_INTERNAL_FOCUS_ONLY",
        }

    def eventFilter(self, watched, event) -> bool:
        if watched is self.command and event.type() == QEvent.Type.FocusOut and self._machine and not self._workspace_dialog_open:
            QTimer.singleShot(0, self._restore_command_focus)
        return super().eventFilter(watched, event)

    def _restore_command_focus(self) -> None:
        if (
            self._workspace_dialog_open
            or self.isMinimized()
            or not self.isActiveWindow()
        ):
            return
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

            self._flush_human_text()
            new_workspace = Workspace.open(selected_path)
            old_service = self.service
            new_service = CommandService(new_workspace)
            new_service.machine = self._machine
            new_service.output = old_service.output
            new_service.state_store.data["machine_mode"] = self._machine
            new_service.state_store.data["output_mode"] = new_service.output
            new_service._persist_state()
            self.service = new_service
            self.service.browser.set_focus_window_handle_provider(lambda: int(self.winId()))
            old_service.close()

            self._active_path = None
            self.editor.clear()
            self.editor.document().setModified(False)
            self.tree.clear()
            if not self._machine:
                self._populate_tree()
            self._load_human_text()
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
                "o Bloco de Texto MCP, configurações internas do EasyChange e temporários próprios.\n\n"
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
            workspace_report = MaintenanceService(workspace_path).purge_all()
            runtime_report = old_service.processes.purge_durable_state()
            report = {
                "mode": "full",
                "removed_files": int(workspace_report.get("removed_files") or 0)
                + int(runtime_report.get("removed_files") or 0),
                "reclaimed_bytes": int(workspace_report.get("reclaimed_bytes") or 0)
                + int(runtime_report.get("reclaimed_bytes") or 0),
                "workspace": workspace_report,
                "external_runtime": runtime_report,
            }
            if not runtime_report.get("ok", False):
                raise RuntimeError(
                    "EXTERNAL_RUNTIME_CLEANUP_FAILED:"
                    + str(runtime_report.get("error") or "unknown")
                )
            new_service = CommandService(Workspace.open(workspace_path))
            new_service.machine = False
            new_service.output = previous_output
            new_service.state_store.data["mode"] = "human"
            new_service.state_store.data["output_mode"] = previous_output
            new_service._persist_state()
            self.service = new_service
            self.service.browser.set_focus_window_handle_provider(lambda: int(self.winId()))
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
            self._load_human_text()
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

    @staticmethod
    def _human_text_status_text(meta: dict, *, prefix: str = "Salvo") -> str:
        size = int(meta.get("bytes") or 0)
        lines = int(meta.get("lines") or 0)
        digest = str(meta.get("sha256") or "")[:12]
        return f"{prefix} • {size / 1024:.1f} KiB • {lines} linhas • SHA {digest}"

    def _load_human_text(self) -> None:
        self._human_text_save_timer.stop()
        self._human_text_loading = True
        try:
            text = self.service.human_text.load()
            self.human_text_editor.setPlainText(text)
            self.human_text_editor.document().setModified(False)
            self._human_text_dirty = False
            self.human_text_status.setText(
                self._human_text_status_text(self.service.human_text.info())
            )
        except Exception as exc:
            self.human_text_editor.clear()
            self._human_text_dirty = False
            self.human_text_status.setText(f"Erro ao carregar: {str(exc)[:180]}")
        finally:
            self._human_text_loading = False

    def _schedule_human_text_save(self) -> None:
        if self._human_text_loading:
            return
        self._human_text_dirty = True
        self.human_text_status.setText("Alterado • salvamento automático em 1,2 s")
        self._human_text_save_timer.start()

    def _autosave_human_text(self) -> None:
        self.save_human_text(show_error=False)

    def save_human_text(self, checked=False, *, show_error: bool = True) -> None:
        del checked
        if self._human_text_loading:
            return
        try:
            meta = self.service.human_text.save(self.human_text_editor.toPlainText())
            self._human_text_dirty = False
            self.human_text_editor.document().setModified(False)
            self.human_text_status.setText(self._human_text_status_text(meta))
        except Exception as exc:
            self._human_text_dirty = True
            self.human_text_status.setText(f"Falha ao salvar: {str(exc)[:180]}")
            if show_error:
                QMessageBox.critical(self, "Falha ao salvar Bloco de Texto MCP", str(exc))

    def _flush_human_text(self) -> None:
        self._human_text_save_timer.stop()
        if self._human_text_dirty:
            self.save_human_text(show_error=False)

    def clear_human_text(self, checked=False) -> None:
        del checked
        answer = QMessageBox.question(
            self,
            "Limpar Bloco de Texto MCP",
            "Apagar todo o texto armazenado para este projeto?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._human_text_save_timer.stop()
        self._human_text_loading = True
        try:
            meta = self.service.human_text.clear()
            self.human_text_editor.clear()
            self.human_text_editor.document().setModified(False)
            self._human_text_dirty = False
            self.human_text_status.setText(
                self._human_text_status_text(meta, prefix="Vazio")
            )
        except Exception as exc:
            QMessageBox.critical(self, "Falha ao limpar Bloco de Texto MCP", str(exc))
        finally:
            self._human_text_loading = False

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


    def _sync_optical_transport_controls(self) -> dict:
        config = self.service.optical_transport_config()
        controls = (
            self.optical_adaptive,
            self.optical_min,
            self.optical_current,
            self.optical_max,
        )
        for control in controls:
            control.blockSignals(True)
        try:
            self.optical_adaptive.setChecked(bool(config["adaptive_enabled"]))
            self.optical_min.setValue(int(config["min_ms"]))
            self.optical_max.setValue(int(config["max_ms"]))
            self.optical_current.setRange(int(config["min_ms"]), int(config["max_ms"]))
            self.optical_current.setValue(int(config["current_ms"]))
        finally:
            for control in controls:
                control.blockSignals(False)
        if config["adaptive_enabled"]:
            self.optical_status.setText(
                f"EC2 ADAPTATIVO • {config['current_ms']} ms "
                f"({config['min_ms']}–{config['max_ms']} ms) • "
                "MCP ajusta conforme HDMI/decoder"
            )
        else:
            self.optical_status.setText(
                f"EC2 MANUAL • {config['current_ms']} ms "
                f"({config['min_ms']}–{config['max_ms']} ms)"
            )
        self.optical_current.setEnabled(not bool(config["adaptive_enabled"]))
        if hasattr(self, "_optical_timer"):
            self._optical_timer.setInterval(int(config["current_ms"]))
        return config

    def _save_optical_transport_controls(self) -> None:
        min_ms = int(self.optical_min.value())
        max_ms = int(self.optical_max.value())
        if min_ms > max_ms:
            if self.sender() is self.optical_min:
                max_ms = min_ms
                self.optical_max.setValue(max_ms)
            else:
                min_ms = max_ms
                self.optical_min.setValue(min_ms)
        current_ms = max(min_ms, min(max_ms, int(self.optical_current.value())))
        self.service.configure_optical_transport(
            adaptive_enabled=bool(self.optical_adaptive.isChecked()),
            min_ms=min_ms,
            max_ms=max_ms,
            current_ms=current_ms,
            adjusted_by="easychange_ui",
        )
        self._sync_optical_transport_controls()
    def _apply_machine_style(self) -> None:
        self.setStyleSheet("QWidget { background:#101820; color:#f2f5f7; font-family: Consolas, monospace; }" if self._machine else "")
        self.tree.setVisible(not self._machine)
        self.select_workspace_button.setVisible(not self._machine)
        self.cleanup_easychange_button.setVisible(not self._machine)
        self.human_text_panel.setVisible(not self._machine)
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
        self._sync_optical_transport_controls()
        focus = self.focus_command()
        if isinstance(result.data, dict):
            result.data["easychange_focus"] = focus
            result.data["command_focus_confirmed"] = bool(
                focus.get("command_focus_confirmed")
            )
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
            # Capture p95 is ~200ms on the physical HDMI path. Keep every
            # page visible for >2 capture periods so repaint/capture/QR decode
            # jitter cannot systematically skip a rotating page.
            self._optical_timer.setInterval(
                int(self.service.optical_transport_config()["current_ms"])
            )
        else:
            self.split.setVisible(True)
            self.output.setVisible(True)
            self.result_panel.setMinimumHeight(0)
            self.result_panel.setMaximumHeight(340 if self._machine else 200)
            # Normal multi-page mode is only used for <=4 visible slots;
            # leave each page up long enough for HDMI capture + QR decode.
            self._optical_timer.setInterval(
                int(self.service.optical_transport_config()["current_ms"])
            )
    def _update_machine_qr(self, result) -> None:
        self._optical_timer.stop()
        self._optical_render_generation += 1
        self._optical_render_pending = None
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

        previous = self._optical_encode_future
        if previous is not None and not previous.done():
            previous.cancel()
        self._optical_encode_future = None

        direct_payloads = _direct_recovery_optical_payloads(result)
        if direct_payloads is not None:
            self._optical_encode_future = None
            self._optical_payloads = direct_payloads
            self._set_optical_burst(
                len(self._optical_payloads) > self._OPTICAL_BURST_THRESHOLD
            )
            self._render_optical_page()
            return

        generation = self._optical_render_generation
        try:
            future = self._optical_encode_executor.submit(
                encode_result_bundle,
                result,
                max_packets=self._OPTICAL_BROADCAST_MAX_CHUNKS,
                overflow_packets=self._OPTICAL_OVERFLOW_BROADCAST_CHUNKS,
            )
        except RuntimeError:
            self._set_optical_burst(False)
            return
        self._optical_encode_future = future
        QTimer.singleShot(
            0,
            lambda g=generation, f=future: self._poll_optical_bundle(g, f),
        )


    def _poll_optical_bundle(self, generation: int, future) -> None:
        if generation != self._optical_render_generation:
            return
        if future is not self._optical_encode_future:
            return
        if not future.done():
            QTimer.singleShot(
                5,
                lambda g=generation, f=future: self._poll_optical_bundle(g, f),
            )
            return
        self._optical_encode_future = None
        try:
            payloads, bundle_header = future.result()
        except (ImportError, ValueError, RuntimeError):
            self._set_optical_burst(False)
            return
        if generation != self._optical_render_generation:
            return
        self._optical_payloads = list(payloads)
        self._set_optical_burst(
            len(self._optical_payloads) > self._OPTICAL_BURST_THRESHOLD
        )
        overflow_handoff = False
        if bundle_header:
            try:
                header = json.loads(bundle_header)
                overflow_handoff = bool(
                    int(header.get("n") or 0) > int(header.get("w") or 0) > 0
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                overflow_handoff = False
        if self._optical_burst or overflow_handoff:
            self._optical_header_payload = bundle_header
        self._render_optical_page()
    def _optical_page_plan(self) -> tuple[int, int, int, int, bool]:
        requested_slots = (
            self._OPTICAL_BURST_SLOTS if self._optical_burst
            else self._OPTICAL_NORMAL_SLOTS
        )
        slots = min(requested_slots, len(self.machine_qrs))
        total = len(self._optical_payloads)
        has_header = bool(self._optical_header_payload)
        if self._optical_burst and has_header:
            first_capacity = max(1, slots - 1)
            tail = max(0, total - first_capacity)
            page_count = 1 + ((tail + slots - 1) // slots if tail else 0)
            page = self._optical_page % max(1, page_count)
            if page == 0:
                return slots, page_count, 0, first_capacity, True
            return slots, page_count, first_capacity + (page - 1) * slots, slots, False
        header_slots = 1 if has_header else 0
        data_slots = max(1, slots - header_slots)
        page_count = max(1, (total + data_slots - 1) // data_slots)
        page = self._optical_page % page_count
        return slots, page_count, page * data_slots, data_slots, has_header

    def _render_optical_page(self) -> None:
        if (
            not self._machine
            or not self._optical_payloads
            or self._optical_render_pending is not None
        ):
            return
        self._optical_timer.stop()
        slots, page_count, start, data_slots, include_header = self._optical_page_plan()
        page = self._optical_page % page_count
        payloads = self._optical_payloads[start:start + data_slots]
        if include_header and self._optical_header_payload:
            payloads = [self._optical_header_payload, *payloads]
        if self._optical_burst and payloads and len(payloads) < slots:
            original = list(payloads)
            index = 0
            while len(payloads) < slots:
                payloads.append(original[index % len(original)])
                index += 1
        self._optical_render_pending = {
            "generation": self._optical_render_generation,
            "page": page, "page_count": page_count, "slots": slots,
            "payloads": list(payloads), "index": 0,
        }
        QTimer.singleShot(0, self._render_next_optical_symbol)

    def _render_next_optical_symbol(self) -> None:
        pending = self._optical_render_pending
        if not isinstance(pending, dict):
            return
        if pending.get("generation") != self._optical_render_generation:
            self._optical_render_pending = None
            return
        payloads = pending.get("payloads") or []
        index = int(pending.get("index") or 0)
        while index < len(payloads) and payloads[index] in self._optical_pixmaps:
            index += 1
        pending["index"] = index
        if index < len(payloads):
            payload = payloads[index]
            self._optical_pixmaps[payload] = self._build_optical_pixmap(payload)
            pending["index"] = index + 1
            QTimer.singleShot(0, self._render_next_optical_symbol)
            return
        self._commit_optical_page(pending)

    def _build_optical_pixmap(self, payload: str) -> QPixmap:
        import qrcode
        code = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=1, border=4, mask_pattern=3,
        )
        code.add_data(payload)
        code.make(fit=True)
        matrix = code.get_matrix()
        height = len(matrix); width = len(matrix[0])
        pixels = bytes(0 if dark else 255 for row in matrix for dark in row)
        image = QImage(
            pixels, width, height, width, QImage.Format.Format_Grayscale8
        ).copy()
        label = self.machine_qrs[0]
        scale = max(1, min(label.width() // width, label.height() // height))
        return QPixmap.fromImage(image).scaled(
            width * scale, height * scale,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )

    def _commit_optical_page(self, pending: dict) -> None:
        if (
            self._optical_render_pending is not pending
            or pending.get("generation") != self._optical_render_generation
        ):
            return
        payloads = list(pending.get("payloads") or [])
        slots = int(pending.get("slots") or 0)
        page = int(pending.get("page") or 0)
        page_count = max(1, int(pending.get("page_count") or 1))
        for label in self.machine_qrs:
            label.clear()
            label.setVisible(False)
        for label, payload in zip(self.machine_qrs[:slots], payloads):
            pixmap = self._optical_pixmaps.get(payload)
            if pixmap is not None:
                label.setPixmap(pixmap)
                label.setVisible(True)
        self._optical_page = (page + 1) % page_count
        self._optical_render_pending = None
        if page_count > 1 and self._machine and self._optical_payloads:
            self._optical_timer.start()

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
        self._flush_human_text()
        future = self._optical_encode_future
        if future is not None and not future.done():
            future.cancel()
        self._optical_encode_executor.shutdown(wait=False, cancel_futures=True)
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
