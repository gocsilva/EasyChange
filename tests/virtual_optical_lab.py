"""Local virtual HID/HDMI/MCP lab for the REAL EasyChange Qt application.

The lab intentionally does not open TCP listeners, COM ports, camera devices,
Bluetooth, tunnels, or the physical MCP service. All mutable workspace state
lives inside TemporaryDirectory; the GUI renders offscreen.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def run(*, mcp_source: str, ocr_binary: str, evidence_dir: str = "",
        optical_width: int = 1280, optical_height: int = 720, max_cases: int = 4, start_case: int = 1) -> dict:
    # Set platform before importing Qt, so no visible/remote GUI is affected.
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = "1"
    import cv2
    import numpy as np
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QFontDatabase, QFont
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from easychange.core.workspace import Workspace
    from easychange.ui.main_window import MainWindow

    # Import the REAL MCP optical reader, with the real EasyChange running
    # inside this subprocess. No call is made to the MCP network/host.
    mcp_root = Path(mcp_source).resolve()
    if not (mcp_root / "mcp_server" / "easychange_tools.py").is_file():
        raise FileNotFoundError("MCP_SOURCE_REQUIRED")
    sys.path.insert(0, str(mcp_root))
    from mcp_server.easychange_tools import (
        _active_machine_mode_status, _decode_ubuntu_ocr_lines
    )
    from capture.local_vision import TesseractCLIProvider

    provider = TesseractCLIProvider(ocr_binary)
    if not provider.available():
        raise RuntimeError("TESSERACT_NOT_AVAILABLE_IN_LAB")
    app = QApplication.instance() or QApplication([])
    loaded=QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\consola.ttf")
    if loaded<0: raise RuntimeError("LAB_FONT_NOT_AVAILABLE")
    evidence = Path(evidence_dir).resolve() if evidence_dir else None
    if evidence:
        evidence.mkdir(parents=True, exist_ok=True)
    measurements = []
    sent_by_esp = 0
    physical_transport_used = False

    with tempfile.TemporaryDirectory(prefix="easychange_virtual_") as temporary:
        project = Path(temporary) / "local_project"
        project.mkdir()
        (project / "program.py").write_text(
            "def greeting(name):\n    return f'Hello {name}'\n\n"
            "def fib(n):\n    if n < 2:\n        return n\n    return fib(n-1) + fib(n-2)\n",
            encoding="utf-8"
        )
        (project / "large.txt").write_text(
            "".join(f"{i:04d} | Simulation line with accented unicode: ação {i}\n"
                    for i in range(1100)), encoding="utf-8"
        )
        window = MainWindow(Workspace.open(project), machine_mode=True, hid_mode=True)
        window.setFixedSize(optical_width, optical_height)
        window.show()
        app.processEvents()
        window.remote_transport_selector.setCurrentText("UBUNTU")
        output_font=QFont("Consolas")
        output_font.setPixelSize(17)
        output_font.setWeight(QFont.Weight.DemiBold)
        window.output.setFont(output_font)
        app.processEvents()

        def screenshot():
            """Virtual HDMI: only Qt framebuffer read, NO real capture device."""
            pixmap = window.grab()
            pixmap = pixmap.scaled(optical_width, optical_height,
                                   Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            qimg = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB888)
            arr = np.frombuffer(qimg.bits(), dtype=np.uint8).reshape(
                qimg.height(), qimg.width(), 3)
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR).copy()

        def send_hid(text: str):
            """Virtual ESP: generate widget keyboard events, not USB input."""
            nonlocal sent_by_esp
            window.command.setFocus(Qt.FocusReason.OtherFocusReason)
            window.command.clear()
            QTest.keyClicks(window.command, text)
            assert window.command.text() == text, "SIMULATED_HID_BYTES_MISMATCH"
            QTest.keyClick(window.command, Qt.Key.Key_Return)
            sent_by_esp += 1
            app.processEvents()

        def wait_receipt(seq: str, timeout_sec=9):
            deadline=time.monotonic()+timeout_sec
            while time.monotonic() < deadline:
                app.processEvents()
                item = window._last_machine_result
                if (item is not None and str(item.sequence or "") == seq and
                        "ECO1" in window.output.toPlainText()):
                    return item
                QTest.qWait(20)
            raise TimeoutError(f"VIRTUAL_EASYCHANGE_RECEIPT_TIMEOUT:{seq}")

        def ocr_read(frame, seq):
            # Same two profiles as production; require checksum and sequence.
            upper=frame[75:min(frame.shape[0],685), :]
            reads=[]
            for profile in ("terminal_code_25x6", "terminal_code_3x6_sharp"):
                result = provider.recognize(upper, timeout=5.0, profile=profile)
                parsed = _decode_ubuntu_ocr_lines(result.text.splitlines(),seq)
                reads.append({"profile":profile,
                              "confidence":round(float(result.confidence or 0),3),
                              "lines":len(result.text.splitlines()),
                              "validated":bool(parsed),
                              "preview":result.text[:180]})
                if parsed:
                    return parsed, reads
            return None,reads

        try:
            for i,command in list(enumerate((
                ":state --compact",
                ":read program.py 1 12",
                ":read large.txt 1 900",
                ":read missing-unknown-path.txt 1 2",
            ), start=1))[start_case-1:start_case-1+max_cases]:
                sequence = f"QLAB{i:04d}"
                wire = f":ec {sequence} :view UBUNTU :force {command}"
                started=time.monotonic()
                send_hid(wire)
                result=wait_receipt(sequence)
                qt_text=window.output.toPlainText()
                exact_receipt=_decode_ubuntu_ocr_lines(qt_text.splitlines(),sequence)
                frame=screenshot()
                if evidence:
                    cv2.imwrite(str(evidence/f"virtual_hdmi_{sequence}.png"),frame)
                header_ocr=provider.recognize(
                    frame[:32,:min(600,frame.shape[1])],timeout=4.0,
                    profile="terminal_code_25x6"
                )
                parsed,attempts=ocr_read(frame,sequence)
                entry={
                    "qt_native_size":[window.width(),window.height()],
                    "sequence":sequence,"command":command,
                    "executed":True,"service_ok":bool(result.ok),
                    "virtual_hid_confirmed":True,
                    "virtual_hdmi_frame":[frame.shape[1],frame.shape[0]],
                    "qt_frame_has_envelope":f"ECO1 UBUNTU {sequence}" in qt_text,
                    "direct_frame_crc_valid":bool(exact_receipt),
                    "receipt_chunk_count":sum(
                        1 for l in qt_text.splitlines()
                        if l.startswith("ECO1 ") and l[5:7].isdigit()),
                    "ocr_focus_valid":_active_machine_mode_status(header_ocr.text),
                    "ocr_focus_sample":header_ocr.text[:160],
                    "ocr_crc_valid":bool(parsed),
                    "ocr_profiles":attempts,
                    "duration_ms":round((time.monotonic()-started)*1000),
                }
                if parsed:
                    entry["decoded_ok"]=parsed.get("ok")
                    entry["decoded_sequence"]=parsed.get("sequence")
                    entry["preview_only"]=bool(
                        (parsed.get("data") or {}).get("payload_truncated"))
                measurements.append(entry)
            return {
                "isolated":True,"physical_transport_used":physical_transport_used,
                "virtual_esp_commands":sent_by_esp,
                "workspace_kind":window.service.workspace.kind,
                "remote_com4_touched":False,"real_hdmi_touched":False,
                "mcp_service_started":False,"reports":measurements
            }
        finally:
            window.close()
            app.processEvents()


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--mcp-source",required=True)
    parser.add_argument("--tesseract",required=True)
    parser.add_argument("--evidence-dir",default="")
    parser.add_argument("--max-cases",type=int,default=4)
    parser.add_argument("--start-case",type=int,default=1)
    args=parser.parse_args()
    report=run(mcp_source=args.mcp_source,ocr_binary=args.tesseract,
               evidence_dir=args.evidence_dir,max_cases=args.max_cases,
               start_case=args.start_case)
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
