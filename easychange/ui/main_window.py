from __future__ import annotations

import base64
import binascii
import json
import os
import time
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from easychange.remote.optical_protocol import (
    MAX_EC2_PACKETS, MAX_EC2_QR_RENDER_BYTES,
    encode_durable_result_header, encode_result_bundle, valid_ec2_identity,
)
from easychange.remote.pixel_transport import encode_pixel_bitmap
from easychange.remote.optical_raster import rasterize_optical_payload
from easychange.remote.gatt_client import EasyChangeGattClient
from easychange.ui.gatt_diagnostic_window import GattDiagnosticWindow

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QColor, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPushButton, QSpinBox,
                               QPlainTextEdit, QSplitter, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from easychange.core.command_service import CommandService
from easychange.core.optical_test_counter import OpticalTestCounter
from easychange.core.maintenance_service import MaintenanceService
from easychange.core.operation_registry import (
    STRUCTURED_MUTATION_TYPES,
    STRUCTURED_RECOVERY_TYPES,
    TEXT_MUTATION_COMMANDS,
)
from easychange.core.result import Result
from easychange.core.workspace import Workspace
from easychange.self_update import RESTART_GRACE_SECONDS, installation_root, schedule_self_update
from easychange.ui.recent_project import remember_project
from easychange.ui.editor import BasicHighlighter, CodeEditor


def _ubuntu_text_frame(result, *, chunk_chars: int = 68,
                       max_chunks: int = 24) -> str:
    """Bounded screen-text receipt with an exact CRC32 and OCR-safe alphabet.

    Uppercase RFC4648 Base32 avoids mixed-case OCR ambiguity at 720p. The
    original durable Result is never edited. Oversized data is represented by
    an explicit verified preview receipt; it is NOT a complete result and
    must be recovered through the durable-result chunk tools.
    """
    import zlib
    sequence=str(getattr(result,"sequence",None) or "-")
    compact=result.render("compact")
    original_size=len(compact.encode("utf-8"))

    def encode(value):
        return base64.b32encode(value.encode("utf-8")).decode("ascii").rstrip("=")

    encoded=encode(compact)
    if len(encoded)>chunk_chars*max_chunks:
        preview=Result(
            ok=bool(result.ok),command=str(result.command),
            command_id=getattr(result,"command_id",None),
            sequence=getattr(result,"sequence",None),
            duration_ms=int(getattr(result,"duration_ms",0) or 0),
            data={"payload_truncated":True,"original_result_bytes":original_size,
                  "recovery":"result_header/result_chunk"},
            error=(str(result.error)[:120] if getattr(result,"error",None) else None),
            code=getattr(result,"code",None),
        )
        compact=preview.render("compact")
        encoded=encode(compact)
    if len(encoded)>chunk_chars*max_chunks:
        # A pathological command ID/metadata must not produce an unreadable
        # partial ECO1 frame. Retain exact sequence with a tiny error receipt.
        fallback=Result(
            ok=False,command="ubuntu_visual_receipt",
            command_id="-",sequence=getattr(result,"sequence",None),
            code="UBUNTU_PREVIEW_TOO_LARGE",
            error="Result available only through durable chunk recovery",
        )
        compact=fallback.render("compact")
        encoded=encode(compact)

    fragments=[encoded[offset:offset+chunk_chars]
               for offset in range(0,len(encoded),chunk_chars)]
    checksum=f"{zlib.crc32(compact.encode('utf-8')) & 0xffffffff:08X}"
    status="OK" if compact.startswith(f"EC1 {sequence} OK") else "ERR"
    lines=[f"ECO1 UBUNTU {sequence} {status} {len(fragments)} B32"]
    lines.extend(f"ECO1 {index:02d} {part}" for index,part in enumerate(fragments))
    lines.append(f"ECO1 END {sequence} {checksum}")
    return "\n".join(lines)


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
    expected_sequence = data.get("target_sequence")
    if expected_sequence is not None and (
        not isinstance(expected_sequence, str) or not expected_sequence
    ):
        return None
    expected_identity = None
    seen_indices: dict[int, str] = {}
    for packet in candidates:
        if not isinstance(packet, dict) or packet.get("p") != "EC2":
            return None
        if not valid_ec2_identity(packet):
            return None
        sequence = packet["s"]
        index, total = packet.get("i"), packet.get("n")
        # Preserve the exact EC2 identity; never render packets that the
        # receiver will reject (invalid metadata, index or per-fragment CRC).
        if (type(index) is not int or type(total) is not int
                or not 1 <= total <= MAX_EC2_PACKETS
                or not 0 <= index < total):
            return None
        if expected_sequence is not None and sequence != expected_sequence:
            return None
        if "t" in packet and (type(packet["t"]) is not int or packet["t"] != 0):
            return None
        identity = (sequence, total, packet["h"], packet["c"], packet.get("e"), packet.get("t"))
        if expected_identity is None:
            expected_identity = identity
        elif identity != expected_identity:
            return None
        piece = packet.get("d")
        if type(piece) is not str:
            return None
        try:
            data_bytes = piece.encode("ascii")
        except UnicodeEncodeError:
            return None
        if any(ch not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in data_bytes):
            return None
        if (type(packet.get("x")) is not str or
                packet["x"] != f"{binascii.crc32(data_bytes) & 0xffffffff:08x}"):
            return None
        try:
            rendered = json.dumps(
                packet, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            )
        except (TypeError, ValueError, OverflowError):
            return None
        # A valid EC2 chunk still needs to fit the actual QR M-level capacity.
        if len(rendered.encode("utf-8")) > MAX_EC2_QR_RENDER_BYTES:
            return None
        previous = seen_indices.get(index)
        if previous is not None:
            # Repeated requested indices must not hide different fragments.
            # An identical retransmission needs only one logical QR; page
            # painting already adds visual redundancy when slots are free.
            if previous != piece:
                return None
            continue
        seen_indices[index] = piece
        payloads.append(rendered)
    return payloads or None


class MainWindow(QMainWindow):
    # Full-HD HDMI landscape: eight columns by four rows, each QR still
    # up to 210 logical pixels. No dense 32-cell reduction to tiny QRs.
    # The optical reader uses QR contours rather than fixed pixel positions.
    _OPTICAL_NORMAL_SLOTS = 4
    _OPTICAL_BURST_SLOTS = 32
    _OPTICAL_PIXMAP_CACHE_ENTRIES = 48
    _OPTICAL_PIXMAP_CACHE_BYTES = 32 * 1024 * 1024
    _OPTICAL_BURST_COLUMNS = 8
    # Larger modules improve dense compact EC2 readability after HDMI scaling.
    _OPTICAL_COMPACT_QR_SIZE = 260
    _OPTICAL_BURST_QR_SIZE = 210  # maximum per-symbol side; scale down on smaller DPI-adjusted desktops
    # A burst page must remain visible for more than two typical HDMI capture
    # periods. This reduces missed pages under paint/capture/decode jitter.
    _OPTICAL_BURST_INTERVAL_MS = 900
    # Bound the initial optical broadcast to one 8x4 page: one EC2H header
    # plus up to 31 data packets. Large results are durable in ResultStore and
    # MCP switches to result_meta/result_chunks after this first page. Rotating
    # a second initial page only added ~1 dwell interval and more QR decode work
    # before the same durable handoff.
    _OPTICAL_BROADCAST_MAX_CHUNKS = 31
    _OPTICAL_OVERFLOW_BROADCAST_CHUNKS = 3
    _STARTUP_FOCUS_RETRY_MS = 100
    _STARTUP_FOCUS_MAX_ATTEMPTS = 30
    # Medium results rotate through the normal 4-slot layout so each QR stays
    # large enough for HDMI decoding. Reserve the dense 32-slot canvas for
    # larger results where a carousel would take too long.
    _OPTICAL_BURST_THRESHOLD = 31

    def __init__(self, workspace: Workspace, *, machine_mode: bool | None = None, hid_mode: bool = False) -> None:
        super().__init__()
        self.service = CommandService(
            workspace,
            defer_initial_maintenance=True,
        )
        self._optical_test_counter = OpticalTestCounter()
        self.service.optical_test_counter = self._optical_test_counter
        self._optical_rendered_wire = OrderedDict()
        # Capture native handle on Qt thread (the worker must not call QWidget.winId).
        native_window_handle = int(self.winId())
        self.service.browser.set_focus_window_handle_provider(
            lambda handle=native_window_handle: handle
        )
        self._machine = self.service.machine if machine_mode is None else machine_mode
        self._update_restart_scheduled = False
        self.service.machine = self._machine
        self._workspace_dialog_open = False
        self._startup_focus_pending = False
        self._startup_focus_attempts = 0

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
        self._tree_population_scheduled = False
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
            label.setFixedSize(
                self._OPTICAL_COMPACT_QR_SIZE,
                self._OPTICAL_COMPACT_QR_SIZE,
            )
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.hide()
            self.machine_qrs.append(label)
        self.machine_qr = self.machine_qrs[0]

        self._last_machine_result = None
        self._optical_payloads: list[str] = []
        self._optical_header_payload: str | None = None
        self._optical_pixmaps: OrderedDict[str, QPixmap] = OrderedDict()
        self._optical_page = 0
        self._optical_burst = False
        self._optical_render_generation = 0
        self._optical_render_pending: dict | None = None
        # A running QR encoder Future cannot be cancelled. With one worker,
        # a stale/large result blocks the current command's EC2 payload until
        # the old work completes, leaving the previous QR page visible.
        # Two bounded workers let the latest result encode independently;
        # _optical_render_generation already drops stale completions.
        self._optical_encode_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="easychange-ec2",
        )
        self._optical_encode_future = None
        # A dedicated pair of raster workers decodes no HDMI and touches no Qt
        # objects. QR bit-matrix generation cannot block the Win32 event loop.
        self._optical_render_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="easychange-qr-raster"
        )
        # A dedicated serial command worker keeps slow HID requests off Qt's
        # paint/event thread without reordering EC1 sequence execution.
        self._command_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="easychange-commands")
        self._command_future = None
        self._active_command = None
        self._machine_command_queue = deque()
        self._close_when_idle = False
        self._command_poll_timer = QTimer(self)
        self._command_poll_timer.setInterval(30)
        self._command_poll_timer.timeout.connect(self._poll_machine_command)
        # Diagnostics are local only. Never open a Bluetooth connection on startup.
        self._gatt_client = EasyChangeGattClient()
        self._gatt_enabled = False
        self.service.gatt_diagnostics_source = self._gatt_client.diagnostics.snapshot
        self._gatt_status = QLabel('GATT: desligado. Clique em Ligar GATT para iniciar.')
        self._gatt_status.setObjectName('easychangeGattStatus')
        # Show the entire failure stage and error instead of clipping the
        # diagnostic to one line. Keep the text copyable for support.
        self._gatt_status.setWordWrap(True)
        self._gatt_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._gatt_diagnostic_window = None
        self.gatt_diagnostic_button = QPushButton("Diagnóstico GATT")
        self.gatt_diagnostic_button.setObjectName("openGattDiagnosticButton")
        self.gatt_diagnostic_button.setToolTip(
            "Abre uma janela separada com historico completo da conexao "
            "Bluetooth GATT, excecoes e informacoes tecnicas, em tempo real.")
        self.gatt_diagnostic_button.clicked.connect(self.open_gatt_diagnostics)
        self.gatt_toggle_button = QPushButton("Ligar GATT")
        self.gatt_toggle_button.setObjectName("toggleGattConnectionButton")
        self.gatt_toggle_button.setToolTip(
            "Liga ou desliga explicitamente o cliente Bluetooth GATT. "
            "Ao desligar, encerra a conexao e o worker, sem alterar USB HID/QR.")
        self.gatt_toggle_button.clicked.connect(self._toggle_gatt_connection)
        self.gatt_retry_button = QPushButton("Tentar GATT")
        self.gatt_retry_button.setObjectName("retryGattConnectionButton")
        self.gatt_retry_button.setToolTip(
            "Nova tentativa de conexao GATT sem remover emparelhamento "
            "nem alterar a configuracao do Windows.")
        self.gatt_retry_button.clicked.connect(self._retry_gatt_connection)
        self.gatt_retry_button.setEnabled(False)
        # Remote operator chooses the route/optical format. Bluetooth remains
        # independently opt-in and never starts merely because the GUI opens.
        self.remote_transport_mode = "AUTO"
        self._host_optical_preference = "QR"
        self._host_forced_ui = False
        self.remote_transport_selector = QComboBox()
        self.remote_transport_selector.setObjectName("easychangeRemoteTransportMode")
        self.remote_transport_selector.addItems(["AUTO", "GATT", "QR", "PIXEL", "PIXEL+QR", "UBUNTU"])
        self.remote_transport_selector.setToolTip(
            "AUTO compara QR, PIXEL e PIXEL+QR em entregas reais, alterna "
            "por latencia e falhas, sem reexecutar comandos. PIXEL+QR mistura "
            "EC2 e ECP1 na mesma pagina; UBUNTU mostra somente texto via OCR, sem QR. GATT permanece uma escolha explicita. "
            "O MCP ajusta o tempo EC2 em cada comando conforme as metricas.")
        self.remote_transport_selector.currentTextChanged.connect(
            self._set_remote_transport_mode)
        gatt_status_row = QHBoxLayout()
        gatt_status_row.addWidget(self._gatt_status, 1)
        gatt_status_row.addWidget(self.remote_transport_selector)
        gatt_status_row.addWidget(self.gatt_toggle_button)
        gatt_status_row.addWidget(self.gatt_retry_button)
        gatt_status_row.addWidget(self.gatt_diagnostic_button)
        self._gatt_timer = QTimer(self)
        self._gatt_timer.setInterval(100)
        self._gatt_timer.timeout.connect(self._poll_gatt_client)
        # The GATT polling timer and BLE worker start only by operator action.
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

        self.command = QLineEdit()
        # Qt defaults QLineEdit.maxLength to 32767 and silently truncates longer
        # HID input. Keep a high defensive ceiling even though large machine
        # payloads now use :u1 segmented staging.
        self.command.setMaxLength(4 * 1024 * 1024)
        self.command.setPlaceholderText("COMMAND > :remote-guide | Ctrl+K | Enter")
        self.command.installEventFilter(self)
        layout.addLayout(header_row); layout.addLayout(gatt_status_row); layout.addWidget(self.optical_controls); layout.addWidget(self.workspace_split, 1); layout.addWidget(self.result_panel); layout.addWidget(self.command)
        self.setCentralWidget(central)
        self.command.returnPressed.connect(self.execute_command)
        self.tree.itemActivated.connect(self.open_selected)
        self.tree.itemDoubleClicked.connect(self.open_selected)
        self.tree.currentItemChanged.connect(self.preview_selected)
        self._active_path: str | None = None

        self._add_shortcut("Ctrl+K", self.focus_command)
        # Operator-initiated GATT retry and read-only diagnostic log through
        # the existing USB HID keyboard, with no mouse calibration dependency.
        self._add_shortcut("Ctrl+Shift+G", self._retry_gatt_connection)
        self._add_shortcut("Ctrl+Shift+D", self.open_gatt_diagnostics)
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

    def _start_machine_display(self) -> None:
        """Enter the dedicated remote-operator display and acquire command focus."""
        self._startup_focus_pending = True
        self._startup_focus_attempts = 0
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(0, self._focus_command_at_startup)

    def _focus_command_at_startup(self) -> None:
        if not self._startup_focus_pending:
            return

        self._startup_focus_attempts += 1
        if self.isActiveWindow():
            self.focus_command()
            if self.command.hasFocus():
                self._startup_focus_pending = False
                return

        if self._startup_focus_attempts >= self._STARTUP_FOCUS_MAX_ATTEMPTS:
            self._startup_focus_pending = False
            return
        QTimer.singleShot(
            self._STARTUP_FOCUS_RETRY_MS,
            self._focus_command_at_startup,
        )

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
            self._switch_workspace_path(selected_path, show_remote=True)
        except (OSError, ValueError, PermissionError) as exc:
            QMessageBox.critical(self, "Não foi possível abrir o projeto", str(exc))
        finally:
            self._workspace_dialog_open = False
            self.focus_command()

    def _switch_workspace_path(self, selected_path: str | Path, *, show_remote: bool) -> None:
        selected_path = Path(selected_path).resolve(strict=True)
        if not selected_path.is_dir():
            raise NotADirectoryError(str(selected_path))
        if selected_path == Path(self.service.workspace.root_path).resolve():
            return

        self._flush_human_text()
        new_workspace = Workspace.open(selected_path)
        old_service = self.service
        new_service = CommandService(new_workspace, defer_initial_maintenance=True)
        new_service.machine = self._machine
        new_service.output = old_service.output
        new_service.state_store.data["mode"] = "machine" if self._machine else "human"
        new_service.state_store.data["transport"] = (
            "hid" if self._machine else old_service.state_store.data.get("transport", "local")
        )
        new_service.state_store.data["output_mode"] = new_service.output
        new_service._persist_state()
        self.service = new_service
        self.service.browser.set_focus_window_handle_provider(
            lambda handle=int(self.winId()): handle
        )
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
        try:
            remember_project(selected_path)
        except (OSError, ValueError):
            pass
        if show_remote:
            self._run_command(":remote")

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
            self.service.browser.set_focus_window_handle_provider(
            lambda handle=int(self.winId()): handle
        )
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

    def _schedule_tree_population(self, delay_ms: int = 75) -> None:
        if self._machine or self._tree_population_scheduled:
            return
        if self.tree.topLevelItemCount() > 0:
            return
        self._tree_population_scheduled = True
        QTimer.singleShot(max(0, int(delay_ms)), self._populate_tree_deferred)

    def _populate_tree_deferred(self) -> None:
        self._tree_population_scheduled = False
        if self._machine or self.tree.topLevelItemCount() > 0:
            return
        self._populate_tree()
    def _populate_tree(self) -> None:
        self.tree.clear()
        for item in self.service.files.list_files():
            QTreeWidgetItem(self.tree, [item["path"]])
    def toggle_machine(self) -> None:
        self._machine = not self._machine
        self.service.machine = self._machine
        self.service._persist_state()
        if not self._machine and self.tree.topLevelItemCount() == 0:
            self._schedule_tree_population(delay_ms=0)
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
        self.header.setText(f"EasyChange | {'MACHINE' if self._machine else 'HUMAN'} | {transport} | FOCUS: COMMAND | {self.service.workspace.root_path}")

    def execute_command(self) -> None:
        text = self.command.text(); self.command.clear()
        if self._machine:
            self._queue_machine_command(text)
        else:
            self._run_command(text)

    def _set_remote_transport_mode(self, value: str) -> None:
        """Change only how this GUI presents results, never replay a command.

        The MCP recognizes ECP1 or EC2 QR from HDMI, so a remote operator's
        choice takes effect even if the local selection has not been refreshed.
        """
        chosen = str(value or "").upper()
        if chosen not in {"AUTO", "QR", "PIXEL", "PIXEL+QR", "GATT", "UBUNTU"}:
            return
        self.remote_transport_mode = chosen
        # A human remote selector change releases a previous MCP GUI lock.
        # The next MCP manual operation may explicitly force it again.
        self._host_forced_ui = False
        # An explicit GATT selection is an operator request to enable it;
        # never start Bluetooth on the default AUTO GUI launch.
        if chosen == "GATT" and not self._gatt_enabled:
            self._enable_gatt_connection()
        self._optical_timer.stop()
        self._optical_render_generation += 1
        self._discard_optical_render_jobs()
        self._optical_render_pending = None
        self._optical_pixmaps.clear()
        self._optical_page = 0
        # Existing EC2 packets remain valid and can be repainted without
        # invoking CommandService or executing the original mutation again.
        if self._machine and self._last_machine_result is not None:
            self._update_machine_qr(self._last_machine_result)
        elif self._machine and self._optical_payloads:
            self._render_optical_page()

    def _toggle_gatt_connection(self) -> None:
        if self._gatt_enabled:
            self._disable_gatt_connection()
        else:
            self._enable_gatt_connection()

    def _enable_gatt_connection(self) -> None:
        """Only explicit user action may start BLE scanning/GATT."""
        if self._gatt_enabled:
            return
        if self._gatt_client.status()["running"]:
            self._gatt_status.setText("GATT: aguardando fim da sessao anterior.")
            return
        self._gatt_enabled = True
        self.gatt_toggle_button.setText("Desligar GATT")
        self.gatt_retry_button.setEnabled(True)
        self._gatt_status.setText("GATT: iniciando por solicitacao do operador...")
        self._gatt_timer.start()
        self._gatt_client.start()

    def _disable_gatt_connection(self) -> bool:
        """Tear down only BLE; preserve the independent HID/optical transport."""
        self._gatt_enabled = False
        # Never execute queued GATT commands after operator shutdown.
        # An already-running mutation remains durable and is not interrupted.
        self._machine_command_queue = deque(
            item for item in self._machine_command_queue if not isinstance(item, tuple)
        )
        # Never join a Bluetooth worker on Qt's paint/event thread. Its
        # shutdown is polled by _poll_gatt_client; closeEvent also defers
        # finalization until this returns True.
        stopped = self._gatt_client.stop(timeout=0.0)
        self.gatt_toggle_button.setText("Ligar GATT")
        self.gatt_toggle_button.setEnabled(stopped)
        self.gatt_retry_button.setEnabled(False)
        if stopped:
            self._gatt_timer.stop()
            self._gatt_status.setText("GATT: desligado. USB HID/QR permanece disponivel.")
        else:
            self._gatt_status.setText("GATT: encerrando Bluetooth; aguardando worker...")
            self._gatt_timer.start()
        return stopped

    def _retry_gatt_connection(self) -> None:
        """Manual non-destructive retry; keyboard shortcut may also enable GATT."""
        if not self._gatt_enabled:
            self._enable_gatt_connection()
        elif not self._gatt_client.retry_now():
            self._gatt_status.setText("GATT: cliente Bluetooth nao esta ativo.")

    def open_gatt_diagnostics(self) -> None:
        """Operator-requested, read-only diagnostics; never starts BLE I/O."""
        dialog = self._gatt_diagnostic_window
        if dialog is None:
            dialog = GattDiagnosticWindow(self)
            self._gatt_diagnostic_window = dialog
        dialog.refresh(self._gatt_client.diagnostics)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _poll_gatt_client(self) -> None:
        """Qt-thread adapter for a GATT link; never requests keyboard/window focus."""
        dialog = getattr(self, "_gatt_diagnostic_window", None)
        if dialog is not None and dialog.isVisible():
            dialog.refresh(self._gatt_client.diagnostics)
        if not self._gatt_enabled:
            if not self._gatt_client.status()["running"]:
                self._gatt_client.stop(timeout=0.0)
                self._gatt_timer.stop()
                self.gatt_toggle_button.setEnabled(True)
                self._gatt_status.setText("GATT: desligado. USB HID/QR permanece disponivel.")
            return
        import queue
        for _ in range(8):
            try:
                item = self._gatt_client.events.get_nowait()
            except queue.Empty:
                break
            if item.get("type") == "pair":
                self._gatt_status.setText(
                    "GATT - Compare este codigo no PC local: "
                    + str(item.get("sas") or "??????")
                )
            elif item.get("type") == "status":
                message = str(item.get("message") or "GATT")
                self._gatt_status.setText(message)
                self._gatt_status.setToolTip(message)
        for _ in range(4):
            try:
                sequence, text = self._gatt_client.commands.get_nowait()
            except queue.Empty:
                break
            if len(self._machine_command_queue) >= 128:
                self._gatt_client.publish_result(sequence, {
                    "ok": False, "command": "ec", "sequence": sequence,
                    "error": "COMMAND_QUEUE_FULL", "code": "COMMAND_QUEUE_FULL",
                })
            else:
                self._machine_command_queue.append((text, sequence))
                self._start_next_machine_command()
    def _queue_machine_command(self, text: str) -> None:
        """Queue EC1 bytes unchanged; never repaint an unfinished response.

        Visual hints belong to the command being DEQUEUED, not the command
        arriving while an earlier optical receipt is still on HDMI. Changing
        the renderer on enqueue can replace a prior sequence's EC2 cells and
        make its MCP correlation fail with RESULT_NOT_CORRELATED.
        """
        if not text.strip():
            return
        if len(self._machine_command_queue) >= 128:
            self.optical_status.setText("EC1 COMMAND QUEUE FULL - RETRY")
            return
        self._machine_command_queue.append(text)
        self._start_next_machine_command()

    def _activate_machine_display_hint(self, text: str) -> str | None:
        """Unwrap display-only EC1 directives once, immediately before execute.

        Never change the sequence, command body or at-most-once journal.
        Unknown or malformed control envelopes fail closed and are not sent.
        """
        parts = text.strip().split(maxsplit=4)
        inline_ms = None
        forced = False
        automatic = False
        if len(parts) >= 3 and parts[2].casefold() == ":view":
            if (len(parts) != 5 or parts[0].casefold() != ":ec"
                    or parts[3].upper() not in {"QR", "PIXEL", "HYBRID", "UBUNTU"}):
                return None
            preference = parts[3].upper()
            command = parts[4]
            if command.startswith(":force "):
                forced, command = True, command[len(":force "):]
            elif command.startswith(":auto "):
                automatic, command = True, command[len(":auto "):]
            if command.startswith(":dwell "):
                dwell_parts = command.split(maxsplit=2)
                if len(dwell_parts) != 3:
                    return None
                try:
                    candidate = int(dwell_parts[1])
                    if not 300 <= candidate <= 5000:
                        return None
                except (ValueError, TypeError):
                    return None
                inline_ms = candidate
                command = dwell_parts[2]
            if not command or command.startswith((":force ", ":auto ", ":dwell ")):
                return None
            text = f":ec {parts[1]} {command}"
        else:
            preference = None
        if inline_ms is not None:
            current = self.service.optical_transport_config()
            if current["adaptive_enabled"]:
                target = max(current["min_ms"], min(current["max_ms"], inline_ms))
                if target != current["current_ms"]:
                    self.service.configure_optical_transport(
                        current_ms=target, adjusted_by="mcp_inline_auto"
                    )
                    self._sync_optical_transport_controls()
        if preference is not None:
            changed = preference != self._host_optical_preference
            self._host_optical_preference = preference
            if forced or (automatic and self._host_forced_ui):
                # A forced MCP choice is applied to the ACTUAL EasyChange GUI
                # before the command runs; it is not merely a pending hint.
                chosen = ("PIXEL+QR" if preference == "HYBRID" else preference) if forced else "AUTO"
                if self.remote_transport_mode != chosen:
                    was_blocked = self.remote_transport_selector.blockSignals(True)
                    try:
                        self.remote_transport_selector.setCurrentText(chosen)
                    finally:
                        self.remote_transport_selector.blockSignals(was_blocked)
                    self._set_remote_transport_mode(chosen)
                self._host_forced_ui = forced
            if changed and self.remote_transport_mode == "AUTO":
                # Freeze the old EC2 receipt exactly as painted. Do not re-
                # encode a stale result in a different visual mode just before
                # a new command starts; only its OWN receipt gets the new mode.
                self._optical_timer.stop()
                self._optical_render_generation += 1
                # Switching the visual codec invalidates all already
                # scheduled QR/PIXEL raster work, not just the Qt page.
                # Otherwise old futures occupy both workers and delay the
                # first correlated receipt of the NEW EC1 sequence.
                self._discard_optical_render_jobs()
                self._optical_render_pending = None
                self._optical_pixmaps.clear()
                self._optical_page = 0
        return text

    def _start_next_machine_command(self) -> None:
        if self._command_future is not None:
            return
        if not self._machine_command_queue:
            self._command_poll_timer.stop()
            if self._close_when_idle:
                QTimer.singleShot(0, self.close)
            return
        while self._machine_command_queue:
            pending = self._machine_command_queue.popleft()
            if isinstance(pending, tuple):  # GATT is independent of HDMI.
                self._active_command = pending
                to_execute = pending[0]
            else:
                to_execute = self._activate_machine_display_hint(pending)
                if to_execute is None:
                    self.optical_status.setText("EC1 INVALID DISPLAY HINT")
                    continue
                self._active_command = to_execute
            self._command_future = self._command_executor.submit(
                self.service.execute, to_execute
            )
            self._command_poll_timer.start()
            return
        self._active_command = None
        self._command_poll_timer.stop()

    def _poll_machine_command(self) -> None:
        future = self._command_future
        if future is None or not future.done():
            return
        self._command_poll_timer.stop()
        pending = self._active_command
        is_gatt = isinstance(pending, tuple)
        text = pending[0] if is_gatt else (pending or "")
        gatt_sequence = pending[1] if is_gatt else None
        self._active_command = None
        self._command_future = None
        try:
            result = future.result()
        except Exception as exc:
            # The durable result journal controls recovery of unknown writes.
            parts = text.split(maxsplit=2)
            sequence = parts[1] if len(parts) == 3 and parts[0].casefold() == ":ec" else None
            result = Result(
                False, "ec", error=f"{type(exc).__name__}: {str(exc)[:256]}",
                code="COMMAND_EXECUTION_FAILED", sequence=sequence,
            )
        try:
            if is_gatt:
                # No optical rendering and no focus_command(): physical mouse
                # and keyboard remain entirely under the operator's control.
                self._gatt_client.publish_result(gatt_sequence, result.to_wire_dict())
            else:
                self._run_command(text, precomputed_result=result)
        finally:
            self._start_next_machine_command()

    def _run_command(self, text: str, *, precomputed_result=None) -> None:
        raw = text.strip()
        sequence = None
        if raw.casefold().startswith(":ec "):
            parts = raw.split(maxsplit=2)
            if len(parts) == 3:
                sequence, raw = parts[1], parts[2]
        is_update_command = raw.lstrip(":").strip().casefold() in {
            "self-update",
            "autonomous-update",
        }
        if precomputed_result is not None:
            result = precomputed_result
        elif is_update_command and self._has_unsaved_editor_changes(discard_easychange=True):
            result = Result(
                False,
                "self-update",
                error="Save the current editor buffer before updating EasyChange.",
                code="UNSAVED_EDITOR_CHANGES",
                sequence=sequence,
            )
        elif (
            raw.lstrip(":").strip().casefold().startswith("switch-project ")
            and self._has_unsaved_editor_changes()
        ):
            result = Result(
                False,
                "switch-project",
                error="Save the current editor buffer before switching projects.",
                code="UNSAVED_EDITOR_CHANGES",
                sequence=sequence,
            )
        else:
            result = self.service.execute(text)
        if result.ok and result.data.get("update_request") == "github_main":
            if getattr(self, "_update_restart_scheduled", False):
                result.data = {
                    "scheduled": True,
                    "duplicate_request": True,
                    "restart_mode": "machine_hid",
                }
            else:
                handoff = schedule_self_update(
                    self.service.workspace.root_path,
                    parent_pid=os.getpid(),
                )
                if handoff.get("ok"):
                    self._update_restart_scheduled = True
                    # Keep the restart receipt to one optical packet where
                    # possible. A multi-page result can outlive the handoff
                    # grace period and lose its final HDMI acknowledgement.
                    result.data = {
                        "scheduled": True,
                        "restart_mode": str(
                            handoff.get("restart_mode") or "machine_hid"
                        ),
                    }
                else:
                    result.ok = False
                    result.code = str(handoff.get("error") or "SELF_UPDATE_FAILED")
                    result.error = str(handoff.get("detail") or handoff.get("error"))
        if result.ok and result.data.get("switch_project"):
            try:
                self._switch_workspace_path(result.data["switch_project"], show_remote=False)
            except (OSError, ValueError, PermissionError) as exc:
                result.ok = False
                result.code = "PROJECT_SWITCH_FAILED"
                result.error = str(exc)
        if self._machine != self.service.machine:
            self._machine = self.service.machine
            self._apply_machine_style()
        # All sequenced replies are already owned by the durable result store.
        # Publish the EC2 receipt *before* any focus or widget refresh, not only
        # for read/state: structured read_many, search, write receipts and
        # recovery requests otherwise leave the previous QR on HDMI while
        # unrelated UI work completes. Never mutate the journaled wire result.
        early_result_optics = bool(self._machine and result.sequence)
        if early_result_optics:
            self._update_machine_qr(result)
        self._refresh_header()
        self._sync_optical_transport_controls()
        focus = self.focus_command()
        if isinstance(result.data, dict):
            # Sequenced native reads were already persisted by CommandService.
            # Appending GUI focus diagnostics afterwards changes the optical
            # EC2 payload and digest relative to the durable result, and
            # needlessly inflates a 1-line answer into several dense QR cells.
            # Keep verbose focus diagnostics for human/unsequenced commands.
            # Every sequenced result is immutable after journal commit:
            # appending focus diagnostics here changes the EC2 checksum,
            # breaks durable replay equivalence and increases QR payload.
            if not is_update_command and not result.sequence:
                result.data["easychange_focus"] = focus
                result.data["command_focus_confirmed"] = bool(
                    focus.get("command_focus_confirmed")
                )
        if not (self._machine and result.sequence and (
            getattr(self, "remote_transport_mode", "AUTO") == "UBUNTU" or
            (getattr(self, "remote_transport_mode", "AUTO") == "AUTO" and
             getattr(self, "_host_optical_preference", "QR") == "UBUNTU")
        )):
            self.output.setPlainText(result.render(self.service.output))
        if not early_result_optics:
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
        if getattr(self, "_update_restart_scheduled", False):
            QTimer.singleShot(RESTART_GRACE_SECONDS * 1000, self.close)

    def _has_unsaved_editor_changes(self, *, discard_easychange: bool = False) -> bool:
        """Return true when a dirty editor has content that differs from disk."""
        if not self.editor.document().isModified():
            return False
        text = self.editor.toPlainText()
        if not self._active_path:
            return bool(text)

        try:
            target = self.service.workspace.resolve(self._active_path, must_exist=True)
            # Never treat unsaved source files inside the EasyChange
            # installation as disposable. Restart/update would discard the
            # buffer just as surely as for a workspace outside the checkout.
            return target.read_text(encoding="utf-8-sig") != text
        except (OSError, UnicodeError, ValueError, PermissionError):
            # If the active file cannot be compared safely, keep the edit guard.
            return True

    @classmethod
    def _burst_qr_size_for_screen(cls, screen_width: int, screen_height: int) -> int:
        """Largest uniform square fitting an 8x4 QR grid on a logical display.

        Reserve 200 logical pixels vertically for the command/controls/header
        and 160 horizontally for window chrome. The QR cells remain >=160
        on small screens to preserve minimum decode legibility.
        """
        side_by_width = (max(0, int(screen_width) - 160) - 7 * 4) // 8
        side_by_height = (max(0, int(screen_height) - 200) - 3 * 4) // 4
        return max(1, min(cls._OPTICAL_BURST_QR_SIZE, side_by_width, side_by_height))

    def _set_optical_burst(self, enabled: bool) -> None:
        terminal_ocr = bool(self._machine and (
            getattr(self, "remote_transport_mode", "AUTO") == "UBUNTU" or
            (getattr(self, "remote_transport_mode", "AUTO") == "AUTO" and
             getattr(self, "_host_optical_preference", "QR") == "UBUNTU")
        ))
        enabled = bool(enabled and self._machine and not terminal_ocr)
        self._optical_burst = enabled
        screen = self.screen()
        available = screen.availableGeometry() if screen is not None else None
        qr_size = (
            self._burst_qr_size_for_screen(available.width(), available.height())
            if enabled and available is not None
            else self._OPTICAL_BURST_QR_SIZE if enabled
            else self._OPTICAL_COMPACT_QR_SIZE
        )
        if any(label.width() != qr_size or label.height() != qr_size for label in self.machine_qrs):
            # Old-size pixmaps must never be reused after a DPI/mode change.
            self._optical_pixmaps.clear()
        for label in self.machine_qrs:
            if label.width() != qr_size or label.height() != qr_size:
                label.setFixedSize(qr_size, qr_size)
        if enabled:
            self.split.setVisible(False)
            self.output.setVisible(False)
            self.result_panel.setMinimumHeight(qr_size * 4 + 3 * 4 + 16)
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
        # Text-only HDMI/OCR presentation: occupy the workspace like a terminal.
        # QR and pixel paint remain entirely disabled while UBUNTU is selected.
        self.qr_panel.setVisible(bool(self._machine and not terminal_ocr))
        self.workspace_split.setVisible(not terminal_ocr)
        self.centralWidget().layout().setStretchFactor(self.result_panel, 1 if terminal_ocr else 0)
        if terminal_ocr:
            self.split.setVisible(False)
            self.output.setVisible(True)
            self.output.setMaximumHeight(16777215)
            self.output.setStyleSheet("QPlainTextEdit { background:#101820; color:#f4f6f8; font: 17px Consolas, monospace; }")
            self.result_panel.setMinimumHeight(420)
            self.result_panel.setMaximumHeight(16777215)
        else:
            self.output.setStyleSheet("")
            self.output.setMaximumHeight(330 if self._machine else 190)

    def _cache_optical_pixmap(self, payload: str, pixmap: QPixmap) -> None:
        cache = self._optical_pixmaps
        cache[payload] = pixmap
        cache.move_to_end(payload)
        # QPixmap normally uses 32-bit backing storage. An LRU also bounds
        # the number of images kept for long, rotating EC2 carousels. Never
        # evict the 16 symbols of one page while it is being assembled.
        def retained_bytes() -> int:
            return sum(max(1, picture.width()) * max(1, picture.height()) * 4
                       for picture in cache.values())
        while (len(cache) > self._OPTICAL_PIXMAP_CACHE_ENTRIES
               or retained_bytes() > self._OPTICAL_PIXMAP_CACHE_BYTES):
            if len(cache) <= self._OPTICAL_BURST_SLOTS:
                break
            cache.popitem(last=False)

    def _show_terminal_optical_header_fast(self, result) -> bool:
        """Paint a durable terminal receipt before EC2 payload encoding finishes."""
        sequence = str(getattr(result, "sequence", "") or "")
        if not sequence:
            return False
        try:
            header = self.service.result_store.header(sequence)
            if not isinstance(header, dict) or not bool(header.get("finished")):
                return False
            payload = encode_durable_result_header(header)
            pixmap = self._build_optical_pixmap(payload)
        except (OSError, TypeError, ValueError, RuntimeError):
            return False

        self._optical_header_payload = payload
        self._cache_optical_pixmap(payload, pixmap)
        label = self.machine_qrs[0]
        label.setPixmap(pixmap)
        label.setVisible(True)
        # Provisional durable EC2H also reaches the physical HDMI canvas.
        # Count it even if later replaced by the complete EC2 bundle.
        self._record_painted_qrs([payload])
        return True
    def _pixel_chunk_chars(self) -> int | None:
        """Fit normal EC2 fragment envelopes into the ECP1 56x56 cell.

        The legacy QR adaptive fragment sizing stays byte-for-byte unchanged.
        Only the visual representation of a fresh PIXEL result is re-chunked.
        The durable result digest, sequence and mutation journal are unchanged.
        """
        chosen = getattr(self, "remote_transport_mode", "AUTO")
        visual = (getattr(self, "_host_optical_preference", "QR")
                  if chosen == "AUTO" else chosen)
        return 130 if visual in {"PIXEL", "PIXEL+QR", "HYBRID"} else None

    def _update_machine_qr(self, result) -> None:
        from easychange.core.transport_audit import log
        self._last_machine_result = result
        self._optical_audit_encode_started=time.perf_counter()
        seq=str(getattr(result,"sequence","") or "")
        log("ec2_encode_start",seq,"EASYCHANGE_TO_MCP",
            command_type=str(getattr(result,"command","") or "")[:60],
            mode=str(getattr(self,"remote_transport_mode","AUTO")),
            ok=bool(getattr(result,"ok",False)))
        self._optical_timer.stop()
        self._optical_render_generation += 1
        self._discard_optical_render_jobs()
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
        if (self.remote_transport_mode == "UBUNTU" or
                (self.remote_transport_mode == "AUTO" and self._host_optical_preference == "UBUNTU")):
            # EC1 execution/result-store is untouched. This mode never calls
            # an optical encoder or emits QR/Pixels, even for recovery commands.
            self._set_optical_burst(False)
            # Small, OCR-safe uppercase chunks. The frame is bounded to fit
            # 720p without scrolling the checksum/footer out of view.
            human_output = result.render("text")
            if len(human_output)>16000:
                human_output=human_output[:16000]+"\n[HUMAN PREVIEW LIMITED]"
            self.output.setPlainText(
                _ubuntu_text_frame(result)+"\n\n"+human_output
            )
            self.output.verticalScrollBar().setValue(0)
            return

        previous = self._optical_encode_future
        if previous is not None and not previous.done():
            previous.cancel()
        self._optical_encode_future = None

        if (
            isinstance(getattr(result, "data", None), dict)
            and result.data.get("suppress_optical_broadcast")
        ):
            self._set_optical_burst(False)
            return

        direct_payloads = _direct_recovery_optical_payloads(result)
        self._optical_direct_recovery = direct_payloads is not None
        if direct_payloads is not None:
            self._optical_encode_future = None
            self._optical_payloads = direct_payloads
            self._set_optical_burst(
                len(self._optical_payloads) > self._OPTICAL_BURST_THRESHOLD
            )
            self._render_optical_page()
            return

        # CommandService has already durably completed sequenced results before
        # returning to the UI. Surface that tiny terminal receipt immediately;
        # payload serialization/compression remains on the EC2 worker.
        self._show_terminal_optical_header_fast(result)

        generation = self._optical_render_generation
        # Small terminal results must be encoded and painted before this GUI
        # callback returns. The MCP can otherwise capture the fast EC2H header
        # while a background encoder is still building the payload QRs, then
        # start a recovery round trip before the current result is visible.
        # This is especially harmful for write receipts and result_chunk(s):
        # every recovery query can reproduce the same header-before-payload
        # race. Keep large/unbounded results on the background worker.
        lines = result.data.get("lines") if isinstance(getattr(result, "data", None), dict) else None
        inline_small_read = bool(
            getattr(result, "ok", False)
            and getattr(result, "sequence", None)
            and getattr(result, "command", "") == "read"
            and isinstance(lines, list)
            and 1 <= len(lines) <= 12
            and all(isinstance(line, str) and len(line) <= 180 for line in lines)
        )
        state_data = result.data if isinstance(getattr(result, "data", None), dict) else None
        inline_compact_state = False
        if (
            getattr(result, "ok", False)
            and getattr(result, "sequence", None)
            and getattr(result, "command", "") == "state"
            and isinstance(state_data, dict)
            and isinstance(state_data.get("state"), str)
        ):
            try:
                # Compact state is a bounded health response. Encoding it on
                # the GUI thread avoids leaving the previous EC2 sequence on
                # screen while the MCP's direct observer is waiting for this
                # command's original optical receipt.
                inline_compact_state = (
                    len(json.dumps(
                        state_data, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")) <= 4096
                )
            except (TypeError, ValueError, UnicodeEncodeError):
                inline_compact_state = False
        inline_small_terminal_result = False
        inline_command = str(getattr(result, "command", "") or "").casefold()
        # :j1 wraps even a single read as outer Result(command="structured").
        # Use only its verified inner read type to paint bounded results
        # immediately instead of leaving the prior sequence on HDMI until an
        # encoder Future finishes and the MCP issues a second request.
        inline_kind = inline_command
        if inline_command == "structured" and isinstance(state_data, dict):
            primary = state_data.get("primary")
            results = state_data.get("results")
            if (state_data.get("operation") == "operation"
                    and isinstance(primary, dict)
                    and isinstance(results, list) and len(results) == 1):
                inline_kind = str(primary.get("command") or "").casefold()
        readonly_inline = {
            "state", "health", "command_schema", "discover_many",
            "search", "locate", "study", "definition", "references",
            "read_many", "read_regions", "result_get",
            "capabilities", "optical_transport_get", "transport_audit", "transport_log",
        }
        inline_limit = (
            8192
            if inline_kind in (
                STRUCTURED_RECOVERY_TYPES | {"locate", "search", "read_many",
                                             "discover_many", "command_schema"}
            )
            else 4096
        )
        if (
            getattr(result, "ok", False)
            and getattr(result, "sequence", None)
            and isinstance(state_data, dict)
            and (
                inline_command in (
                    STRUCTURED_MUTATION_TYPES
                    | STRUCTURED_RECOVERY_TYPES
                    | TEXT_MUTATION_COMMANDS
                    | {"locate", "search", "read_many"}
                )
                or inline_kind in readonly_inline
            )
        ):
            try:
                inline_small_terminal_result = len(json.dumps(
                    state_data, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")) <= inline_limit
            except (TypeError, ValueError, UnicodeEncodeError):
                inline_small_terminal_result = False
        if inline_small_read or inline_compact_state or inline_small_terminal_result:
            try:
                payloads, header = encode_result_bundle(
                    result,
                    chunk_chars=self._pixel_chunk_chars(),
                    max_packets=self._OPTICAL_BROADCAST_MAX_CHUNKS,
                    overflow_packets=self._OPTICAL_OVERFLOW_BROADCAST_CHUNKS,
                )
            except (ValueError, TypeError, OverflowError, RuntimeError):
                # Preserve the durable receipt; the normal async branch still
                # handles unexpected encoder failures.
                pass
            else:
                completed = Future()
                completed.set_result((payloads, header))
                self._optical_encode_future = completed
                self._poll_optical_bundle(generation, completed)
                if inline_compact_state or inline_small_terminal_result:
                    # Complete the small result's QR page(s) before releasing
                    # the command callback. Merely scheduling paint work left
                    # the terminal header visible while payload symbols were
                    # still absent from fresh remote MCP captures.
                    for _ in range(len(self.machine_qrs) + 1):
                        if self._optical_render_pending is None:
                            break
                        # Preserve immediate paint for verified small terminal
                        # receipts; async raster applies to non-inline pages.
                        self._render_next_optical_symbol(force_inline=True)
                    # QLabel.setPixmap schedules a paint for the next event
                    # loop turn. The MCP starts observing as soon as the HID
                    # submission is acknowledged, so flush the compact state
                    # cells while this GUI callback still owns the event loop.
                    for label in self.machine_qrs:
                        if label.isVisible():
                            label.repaint()
                return
        try:
            future = self._optical_encode_executor.submit(
                encode_result_bundle,
                result,
                chunk_chars=self._pixel_chunk_chars(),
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
            # The frame dwell is hundreds of milliseconds; checking a
            # background compression Future every 5ms wastes GUI wakeups.
            QTimer.singleShot(
                25,
                lambda g=generation, f=future: self._poll_optical_bundle(g, f),
            )
            return
        self._optical_encode_future = None
        try:
            payloads, bundle_header = future.result()
        except Exception as exc:
            # A failed QR encoding must not propagate uncaught through Qt's
            # event loop; preserve the already-painted durable EC2H receipt.
            self.optical_status.setText(f"EC2 ENCODE ERROR: {type(exc).__name__}")
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
        # Replace the durable-only receipt with the full optical header only
        # when the page plan actually needs one. For small results the normal
        # EC2 payload replaces the provisional receipt on the next commit.
        # Compact two/three-chunk responses have an otherwise idle QR slot.
        # Keep the correlated EC2H receipt visible in that slot without adding
        # a carousel page. The receiver can stop waiting immediately when one
        # payload cell is unreadable, then recover the exact durable result.
        # Single-chunk responses stay minimal; four chunks keep one-page
        # geometry without adding a fifth QR.
        show_compact_receipt = (
            not self._optical_burst
            and 2 <= len(self._optical_payloads) < self._OPTICAL_NORMAL_SLOTS
        )
        self._optical_header_payload = (
            bundle_header
            if (self._optical_burst or overflow_handoff or show_compact_receipt)
            else None
        )
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
            # Keep the terminal receipt visible on EVERY burst page. When the
            # consumer attaches mid-rotation it can immediately correlate the
            # sequence rather than entering a durable-header requery/replay.
            data_slots = max(1, slots - 1)
            page_count = max(1, (total + data_slots - 1) // data_slots)
            page = self._optical_page % page_count
            return slots, page_count, page * data_slots, data_slots, True
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
        # The command worker can update the persisted EC2 dwell while this
        # Qt timer is active. Read it on each page, without process restart.
        self._optical_timer.setInterval(
            int(self.service.optical_transport_config()["current_ms"])
        )
        slots, page_count, start, data_slots, include_header = self._optical_page_plan()
        page = self._optical_page % page_count
        payloads = self._optical_payloads[start:start + data_slots]
        if include_header and self._optical_header_payload:
            payloads = [self._optical_header_payload, *payloads]
        if (self._optical_burst or getattr(self, '_optical_direct_recovery', False)) and payloads and len(payloads) < slots:
            # Redundancy is display-only, preserving unique EC2 indices and
            # the transport's existing logical fragment contract.
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

    def _render_next_optical_symbol(self, *, force_inline: bool = False) -> None:
        pending = self._optical_render_pending
        if not isinstance(pending, dict):
            return
        if pending.get("generation") != self._optical_render_generation:
            self._discard_optical_render_jobs()
            self._optical_render_pending = None
            return
        payloads = pending.get("payloads") or []
        index = int(pending.get("index") or 0)
        while index < len(payloads) and payloads[index] in self._optical_pixmaps:
            self._optical_pixmaps.move_to_end(payloads[index])
            index += 1
        pending["index"] = index
        if index < len(payloads):
            payload = payloads[index]
            try:
                if (len(payloads) > 1 or len(payload) > 256) and not force_inline:
                    # Even 2-4 ordinary QR cells can stall Qt for hundreds of
                    # milliseconds; a single large QR may block as well.
                    # Keep only genuinely tiny one-cell receipts inline.
                    # Prepare at most two distinct rasters concurrently and
                    # convert QImage -> QPixmap ONLY on this Qt thread.
                    futures = pending.setdefault("raster_futures", {})
                    requested = str(self.remote_transport_mode)
                    preference = str(self._host_optical_preference)
                    for candidate in payloads[index:]:
                        if len(futures) >= 2:
                            break
                        if candidate in self._optical_pixmaps or candidate in futures:
                            continue
                        futures[candidate] = self._optical_render_executor.submit(
                            rasterize_optical_payload,
                            candidate, requested, preference,
                            burst=bool(self._optical_burst),
                            label_side=min(self.machine_qrs[0].width(),
                                           self.machine_qrs[0].height()),
                        )
                    future = futures.get(payload)
                    if future is None or not future.done():
                        QTimer.singleShot(16, self._render_next_optical_symbol)
                        return
                    pixels, side, kind, wire = future.result()
                    self._remember_optical_wire(payload, wire, kind)
                    self._cache_optical_pixmap(
                        payload, self._pixmap_from_optical_raster(pixels, side),
                    )
                    futures.pop(payload, None)
                else:
                    # Compact status receipts use the proven low-latency path.
                    self._cache_optical_pixmap(
                        payload, self._build_optical_pixmap(payload),
                    )
            except Exception as exc:
                self._discard_optical_render_jobs()
                self._optical_render_pending = None
                self._optical_payloads = []
                self._optical_timer.stop()
                previous = self._optical_encode_future
                if previous is not None and not previous.done():
                    previous.cancel()
                self._optical_encode_future = None
                self._set_optical_burst(False)
                self.optical_status.setText(f"EC2 RENDER ERROR: {type(exc).__name__}")
                return
            pending["index"] = index + 1
            QTimer.singleShot(0, self._render_next_optical_symbol)
            return
        self._commit_optical_page(pending)

    def _discard_optical_render_jobs(self) -> None:
        pending = self._optical_render_pending
        if isinstance(pending, dict):
            for future in pending.get("raster_futures", {}).values():
                if not future.done():
                    future.cancel()

    def _remember_optical_wire(self, original: str, encoded: str, kind: str) -> None:
        mapping = self._optical_rendered_wire
        mapping[original] = (encoded, kind)
        mapping.move_to_end(original)
        while len(mapping) > 64:
            mapping.popitem(last=False)

    def _record_painted_qrs(self, originals: list[str]) -> None:
        qr_payloads = []
        pixel_count = 0
        for original in originals:
            wire, kind = self._optical_rendered_wire.get(original, (original, "QR"))
            if kind == "QR":
                qr_payloads.append(wire)
            else:
                pixel_count += 1
        self._optical_test_counter.record(qr_payloads)
        if pixel_count:
            self._optical_test_counter.mark_non_qr(pixel_count)

    def _pixmap_from_optical_raster(self, pixels: bytes, side: int) -> QPixmap:
        # QWidget/QPixmap must never be created or used in the worker.
        image = QImage(
            pixels, side, side, side, QImage.Format.Format_Grayscale8
        ).copy()
        label = self.machine_qrs[0]
        return QPixmap.fromImage(image).scaled(
            label.width(), label.height(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )

    def _build_optical_pixmap(self, payload: str) -> QPixmap:
        # Compact/first receipt: synchronous, preserving the legacy API.
        # Large 8x4 pages use this SAME lossless rasterizer in workers.
        pixels, side, kind, wire = rasterize_optical_payload(
            payload,
            str(getattr(self, "remote_transport_mode", "AUTO")),
            str(getattr(self, "_host_optical_preference", "QR")),
            burst=bool(self._optical_burst),
            label_side=min(self.machine_qrs[0].width(), self.machine_qrs[0].height()),
        )
        self._remember_optical_wire(payload, wire, kind)
        # Support the existing minimal Qt test adapters which invoke this
        # method with a duck-typed widget carrier rather than MainWindow.
        return MainWindow._pixmap_from_optical_raster(self, pixels, side)

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
        shown = []
        for label, payload in zip(self.machine_qrs[:slots], payloads):
            pixmap = self._optical_pixmaps.get(payload)
            if pixmap is not None:
                label.setPixmap(pixmap)
                label.setVisible(True)
                shown.append(payload)
        self._record_painted_qrs(shown)
        # One record per painted PAGE, not per 60Hz screen refresh.
        # The payload hashes are useful for diagnosing QR/PIXEL decoding
        # while keeping source code and result data out of durable logs.
        from easychange.core.transport_audit import log
        seq=""
        for payload in shown[:1]:
            try:
                packet=json.loads(payload)
                seq=str(packet.get("s") or "")
            except (ValueError,TypeError,AttributeError):
                pass
        qr_count=sum(1 for value in shown
                    if self._optical_rendered_wire.get(value,(value,"QR"))[1]=="QR")
        from easychange.core.transport_audit import signature
        manifest=signature(json.dumps(shown,ensure_ascii=True,separators=(",",":")))
        started_at=getattr(self,"_optical_audit_encode_started",None)
        log("ec2_page_painted",seq,"EASYCHANGE_TO_MCP",
            mode=str(getattr(self,"remote_transport_mode","AUTO")),
            page=page,pages=page_count,packets=len(shown),
            chunks=qr_count,frames=len(shown)-qr_count,
            sha256=manifest["sha256"],bytes=manifest["bytes"],
            render_ms=round((time.perf_counter()-started_at)*1000,2)
                      if isinstance(started_at,(int,float)) else None,
            generation=self._optical_render_generation)
        self._optical_page = (page + 1) % page_count
        self._optical_render_pending = None
        if page_count > 1 and self._machine and self._optical_payloads:
            self._optical_timer.start()

    def _prompt(self, value: str) -> None:
        self.command.setText(value); self.focus_command(); self.command.setCursorPosition(len(value))

    def preview_selected(self, current, previous=None) -> None:
        if current:
            # _load_editor only needs a path. FileService.read() would first
            # load, decode, hash and cache the entire file, even when the
            # editor will reject it as larger than 2 MiB.
            try: self._load_editor({"path": current.text(0)})
            except (OSError, ValueError): pass

    def open_selected(self, item, column=0) -> None:
        self.preview_selected(item)
        self.focus_command()

    def _load_editor(self, data: dict) -> None:
        path = data.get("path")
        if not path:
            return
        target = self.service.workspace.resolve(path, must_exist=True)
        limit = 2 * 1024 * 1024
        if target.stat().st_size > limit:
            self._active_path = None
            self.editor.clear()
            self.output.setPlainText("FILE_TOO_LARGE: editor limit is 2 MiB; use :read with a line range")
            return
        # Bound the descriptor read too: a file can grow after stat().
        # Keep FileService's binary-file rejection when loading directly.
        with target.open("rb") as source:
            raw = source.read(limit + 1)
        if len(raw) > limit:
            self._active_path = None
            self.editor.clear()
            self.output.setPlainText("FILE_TOO_LARGE: editor limit is 2 MiB; use :read with a line range")
            return
        if b"\0" in raw:
            raise ValueError("Binary files cannot be edited as text")
        content = raw.decode("utf-8-sig")
        # The edit baseline must describe exactly what is displayed, even if
        # another process replaces the file between our read and this call.
        self.service.files.remember(path, replace=True, snapshot_bytes=raw)
        self._active_path = path
        self.editor.setPlainText(content)
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
        # Stop accepting GATT work before waiting for in-flight durable commands.
        # If Windows is still releasing BLE, defer closing rather than leak it.
        if not self._disable_gatt_connection():
            event.ignore()
            QTimer.singleShot(300, self.close)
            return
        self._flush_human_text()
        future = self._optical_encode_future
        if future is not None and not future.done():
            future.cancel()
        # A background command may still be persisting a durable mutation
        # receipt. Never close its service underneath the worker.
        if self._command_future is not None or self._machine_command_queue:
            self._close_when_idle = True
            event.ignore()
            return
        self._command_poll_timer.stop()
        self._gatt_timer.stop()
        if self._gatt_diagnostic_window is not None:
            self._gatt_diagnostic_window.close()
            self._gatt_diagnostic_window = None
        self.service.gatt_diagnostics_source = None
        self._command_executor.shutdown(wait=False, cancel_futures=True)
        self._discard_optical_render_jobs()
        self._optical_render_executor.shutdown(wait=False, cancel_futures=True)
        self._optical_encode_executor.shutdown(wait=False, cancel_futures=True)
        self.service.close()
        super().closeEvent(event)

    @staticmethod
    def _quote(value: str) -> str:
        return '"' + value.replace('"', '\\"') + '"'


def run(workspace_path: str = ".", *, machine_mode: bool | None = None, hid_mode: bool = False) -> int:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(Path(workspace_path)), machine_mode=machine_mode, hid_mode=hid_mode)
    if window._machine:
        window._start_machine_display()
    else:
        window.show()
    try:
        remember_project(workspace_path)
    except (OSError, ValueError):
        pass
    if not window._machine:
        # Let Qt paint the human GUI before enumerating workspace files.
        window._schedule_tree_population(delay_ms=75)
    # Automatic cleanup is housekeeping, not a precondition for showing the
    # window or accepting commands. Start it only after Qt has had a chance to
    # paint the GUI.
    QTimer.singleShot(100, window.service.start_deferred_maintenance)
    return app.exec()
