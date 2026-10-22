"""Single non-modal, text-only live BLE/GATT diagnostics window.

The GUI reads a bounded copy of connection diagnostics from the existing
Bluetooth worker. This widget has no access to secret payloads, no
permission to change settings, and never starts a second BLE connection.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
)


class GattDiagnosticWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("EasyChange - Diagnostico detalhado GATT")
        self.setObjectName("gattDiagnosticWindow")
        self.resize(920, 610)
        self.setMinimumSize(630, 340)
        self.setModal(False)
        layout = QVBoxLayout(self)
        help_text = QLabel(
            "Historico ao vivo desde a inicializacao do EasyChange, com milissegundos. "
            "Somente metadados Bluetooth/GATT e erros; sem senhas, codigo SAS ou conteudo de comandos."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.log_text = QPlainTextEdit()
        self.log_text.setObjectName("gattDiagnosticText")
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(1200)
        self.log_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_text.setStyleSheet(
            "QPlainTextEdit {color: #E8F2FA; background: #101B27;"
            "font-family: Consolas, monospace; font-size: 12px;}"
        )
        layout.addWidget(self.log_text, 1)
        buttons = QHBoxLayout()
        self.copy_button = QPushButton("Copiar log")
        self.copy_button.setObjectName("gattCopyLogButton")
        self.copy_button.clicked.connect(self.copy_log)
        self.save_button = QPushButton("Salvar log .txt")
        self.save_button.setObjectName("gattSaveLogButton")
        self.save_button.clicked.connect(self.save_log)
        self.close_button = QPushButton("Fechar")
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.save_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self._cursor = 0
        self.setStyleSheet(
            "QDialog {background: #F1F4F8; color: #141E2B;} "
            "QLabel {color: #141E2B;} QPushButton {color: #141E2B;"
            "background: #DFE8F2; padding: 8px; border: 1px solid #8596AC;}"
        )

    def refresh(self, trace) -> None:
        """Append unread events in order; automatically follow the newest."""
        for _ in range(4):  # Drain at most 4 pages per GUI timer tick.
            batch = trace.snapshot(self._cursor, limit=150)
            if not self._cursor and batch["oldest"] > 1:
                self.log_text.appendPlainText(
                    "[Historico anterior expirou do limite em memoria]")
            elif batch["dropped"]:
                self.log_text.appendPlainText(
                    f"[{batch['dropped']} eventos antigos foram descartados]")
            events = batch["events"]
            if not events:
                break
            for event in events:
                self.log_text.appendPlainText(
                    f"[{event['timestamp']}] [{event['stage']}]"
                    f" #{event['id']}\n{event['message']}\n"
                )
            self._cursor = batch["cursor"]
            if len(events) < 150:
                break
        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum())

    def copy_log(self) -> None:
        QApplication.clipboard().setText(self.log_text.toPlainText())

    def save_log(self) -> None:
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Salvar diagnostico GATT", "easychange-gatt-diagnostico.txt",
            "Arquivo texto (*.txt)")
        if not filepath:
            return
        try:
            Path(filepath).write_text(
                self.log_text.toPlainText(), encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            QMessageBox.warning(
                self, "Nao foi possivel salvar",
                f"O Windows recusou salvar o arquivo: {type(exc).__name__}")
