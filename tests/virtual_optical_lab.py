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


def run(*, mcp_source: str = "", ocr_binary: str = "tesseract", evidence_dir: str = "",
        optical_width: int = 1280, optical_height: int = 720, max_cases: int = 4, start_case: int = 1,
        easychange_source: str = "", transport_mode: str = "UBUNTU") -> dict:
    # Import only the explicitly selected local checkout. This prevents an
    # older/dirty canonical tree from being mistaken for the tested main.
    if easychange_source:
        selected=Path(easychange_source).resolve()
        if not (selected/"easychange"/"ui"/"main_window.py").is_file():
            raise FileNotFoundError("EASYCHANGE_SOURCE_NOT_FOUND")
        sys.path.insert(0,str(selected))
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

    # True MCP decoder is used when its private source is accessible.
    # Public EasyChange CI uses the independent published wire-contract
    # oracle. The private source is NEVER fetched into public CI.
    if mcp_source:
        mcp_root=Path(mcp_source).resolve()
        if not (mcp_root/"mcp_server"/"easychange_tools.py").is_file():
            raise FileNotFoundError("MCP_SOURCE_REQUIRED")
        sys.path.insert(0,str(mcp_root))
        from mcp_server.easychange_tools import (
            _active_machine_mode_status, _decode_ubuntu_ocr_lines
        )
        from capture.local_vision import TesseractCLIProvider
        from mcp_server.easychange_tools import (
            _decode_zxing_ec2_frame, _assemble_ec2_packets, _parse_result_text,
        )
        from app.easychange_pixel_transport import decode_pixel_frame
        provider=TesseractCLIProvider(ocr_binary)
        decoder="real-private-mcp"
    else:
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from virtual_optical_contract import (
            focus_valid as _active_machine_mode_status,
            decode_lines as _decode_ubuntu_ocr_lines,
        )
        import subprocess
        from types import SimpleNamespace
        class PublicTesseractProvider:
            """MCP-equivalent 720p preprocessing, no private code or service."""
            def __init__(self, executable):
                self.executable=executable
            def available(self):
                return bool(self.executable)
            def recognize(self, frame, timeout, profile="terminal_code_25x6"):
                factor=3 if "3x" in profile else 2.5
                gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY) if frame.ndim==3 else frame
                gray=cv2.resize(gray,None,fx=factor,fy=factor,
                                interpolation=cv2.INTER_CUBIC)
                if float(gray.mean())<127:gray=cv2.bitwise_not(gray)
                gray=cv2.threshold(gray,0,255,
                    cv2.THRESH_BINARY+cv2.THRESH_OTSU)[1]
                ok,data=cv2.imencode(".png",gray)
                if not ok:raise RuntimeError("VIRTUAL_OCR_IMAGE_ENCODING_FAILED")
                cmd=[self.executable,"stdin","stdout","-l","eng","--psm","6"]
                out=subprocess.run(cmd,input=data.tobytes(),capture_output=True,
                                   timeout=max(.1,float(timeout)),check=True)
                return SimpleNamespace(text=out.stdout.decode("utf8",errors="replace"),
                                       confidence=0.0)
        provider=PublicTesseractProvider(ocr_binary)
        decoder="public-wire-contract"
    if not provider.available():
        raise RuntimeError("TESSERACT_NOT_AVAILABLE_IN_LAB")
    app = QApplication.instance() or QApplication([])
    font_candidates=(
        Path(r"C:\Windows\Fonts\consola.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf"),
    )
    font_file=next((p for p in font_candidates if p.is_file()),None)
    if font_file is None:raise RuntimeError("LAB_MONOSPACED_FONT_NOT_AVAILABLE")
    loaded=QFontDatabase.addApplicationFont(str(font_file))
    if loaded<0:raise RuntimeError("LAB_FONT_LOAD_FAILED")
    family=QFontDatabase.applicationFontFamilies(loaded)[0]
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
        # Redirect actual EasyChange transport journal away from LOCALAPPDATA
        # in the process running only the simulated GUI.
        from easychange.core import transport_audit
        audit_root=project.parent/"virtual_transport_logs"
        transport_audit.ROOT=audit_root
        transport_audit.FILE=audit_root/"transport_audit.jsonl"
        transport_audit._LAST_ID=0
        window = MainWindow(Workspace.open(project), machine_mode=True, hid_mode=True)
        window.setFixedSize(optical_width, optical_height)
        window.show()
        app.processEvents()
        if transport_mode not in {"UBUNTU","QR","PIXEL","PIXEL+QR"}:
            raise ValueError("LAB_TRANSPORT_MODE_INVALID")
        if transport_mode != "UBUNTU" and not mcp_source:
            raise RuntimeError("PIXEL_AND_QR_REQUIRE_REAL_MCP_DECODER")
        window.remote_transport_selector.setCurrentText(transport_mode)
        output_font=QFont(family)
        output_font.setPixelSize(21)
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
                if item is not None and str(item.sequence or "") == seq:
                    if transport_mode=="UBUNTU":
                        if "ECO1" in window.output.toPlainText():
                            return item
                    else:
                        # Wait for the correct optical sequence, not a stale QR
                        # left behind by a previous command.
                        payloads=getattr(window,"_optical_payloads",()) or ()
                        header=getattr(window,"_optical_header_payload","") or ""
                        if (any(seq in str(p) for p in payloads)
                                or seq in str(header)):
                            for _ in range(8):
                                app.processEvents()
                                QTest.qWait(30)
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
                ":write sandbox_written.txt Value_from_virtual_MCP_20261023",
                ":read sandbox_written.txt 1 4",
                ":search greeting",
                ":search Simulation path:large.txt --limit 5",
            ), start=1))[start_case-1:start_case-1+max_cases]:
                sequence = f"QLAB{i:04d}"
                mode_hint="HYBRID" if transport_mode=="PIXEL+QR" else transport_mode
                wire = f":ec {sequence} :view {mode_hint} :force {command}"
                started=time.monotonic()
                send_hid(wire)
                result=wait_receipt(sequence)
                qt_text=window.output.toPlainText()
                exact_receipt=(
                    _decode_ubuntu_ocr_lines(qt_text.splitlines(),sequence)
                    if transport_mode=="UBUNTU" else None
                )
                frame=screenshot()
                if evidence:
                    cv2.imwrite(str(evidence/f"virtual_hdmi_{sequence}.png"),frame)
                    (evidence/f"virtual_text_{sequence}.txt").write_text(
                        qt_text, encoding="utf8")
                header_ocr=provider.recognize(
                    frame[:32,:min(600,frame.shape[1])],timeout=4.0,
                    profile="terminal_code_25x6"
                )
                if transport_mode=="UBUNTU":
                    parsed,attempts=ocr_read(frame,sequence)
                else:
                    # Capture the real Qt optical page sequence, not a single
                    # arbitrary screenshot. QR/PIXEL/hybrid can need multiple
                    # pages even for a tiny write acknowledgement.
                    chunks={}
                    packet_count={"qr":0,"pixel":0}
                    observed_pages=set()
                    parsed=None
                    integrity={}
                    frame_count=0
                    deadline=time.monotonic()+8.0
                    while time.monotonic()<deadline and frame_count<28:
                        current=frame if frame_count==0 else screenshot()
                        frame_count+=1
                        observed_pages.add(int(getattr(window,"_optical_page",0)))
                        qr=_decode_zxing_ec2_frame(current)
                        if qr:
                            packet_count["qr"]+=len(qr[0])
                        pixels=decode_pixel_frame(current)
                        packet_count["pixel"]+=len(pixels)
                        packets=list(qr[0]) if qr else []
                        for raw_pixel in pixels:
                            try:
                                packet=json.loads(raw_pixel)
                                if isinstance(packet,dict):
                                    packets.append(packet)
                            except (ValueError,TypeError):
                                continue
                        for packet in packets:
                            if (packet.get("p")=="EC2"
                                    and packet.get("s")==sequence
                                    and type(packet.get("i")) is int):
                                chunks[packet["i"]]=packet
                        decoded,integrity=_assemble_ec2_packets(
                            chunks,expected_sequence=sequence,
                        )
                        parsed=_parse_result_text(decoded) if decoded else None
                        if parsed and parsed.get("sequence")==sequence:
                            break
                        parsed=None
                        QTest.qWait(140)
                        app.processEvents()
                    attempts=[{
                        "profile":"mcp_multi_page_qr_pixel_reassembly",
                        "qr_packets":packet_count["qr"],
                        "pixel_packets":packet_count["pixel"],
                        "frames_captured":frame_count,
                        "qt_page_indexes_observed":sorted(observed_pages),
                        "fragment_indices":sorted(chunks),
                        "integrity":{key:val for key,val in integrity.items()
                                     if not key.startswith("_")},
                        "validated":bool(parsed),
                    }]
                # Distinguish the server's filesystem effect from the ACTUAL
                # reply delivered through HDMI OCR to the calling virtual MCP.
                written=project/"sandbox_written.txt"
                disk_after_write=(
                    written.read_text(encoding="utf8") if written.exists() else None
                )
                entry={
                    "decoder":decoder,
                    "transport_mode":transport_mode,
                    "optical_page_painted":bool(
                        getattr(window,"_optical_pixmaps",None)),
                    "optical_payload_count":len(
                        getattr(window,"_optical_payloads",()) or ()),
                    "qt_native_size":[window.width(),window.height()],
                    "sequence":sequence,"command":command,
                    "executed":True,"service_ok":bool(result.ok),
                    "virtual_hid_confirmed":True,
                    "virtual_hdmi_frame":[frame.shape[1],frame.shape[0]],
                    "qt_frame_has_envelope":(
                        f"ECO1 UBUNTU {sequence}" in qt_text
                        if transport_mode=="UBUNTU" else
                        any(sequence in str(x) for x in
                            getattr(window,"_optical_payloads",()) or ())
                    ),
                    "direct_frame_crc_valid":(
                        bool(exact_receipt) if transport_mode=="UBUNTU"
                        else bool(parsed)
                    ),
                    "image_crc_valid":bool(parsed),
                    "optical_decoder":("tesseract_base32" if transport_mode=="UBUNTU"
                                       else "zxing_pixel_crc32_sha256"),
                    "virtual_mcp_received_reply":bool(parsed),
                    "virtual_mcp_received_expected_sequence":bool(
                        parsed and parsed.get("sequence")==sequence),
                    "server_disk_write_observed": (
                        "Value_from_virtual_MCP_20261023" in (disk_after_write or "")
                        if i in {5,6} else None),
                    "receipt_chunk_count":sum(
                        1 for l in qt_text.splitlines()
                        if l.startswith("ECO1 ") and l[5:7].isdigit()),
                    "ocr_focus_valid":_active_machine_mode_status(header_ocr.text),
                    "ocr_focus_sample":header_ocr.text[:160],
                    "ocr_crc_valid":bool(parsed),
                    "ocr_profiles":attempts,
                    "duration_ms":round((time.monotonic()-started)*1000),
                }
                if "search" in command:
                    data=result.data or {}
                    entry["search_match_count"]=len(data.get("matches",[]))
                    entry["search_match_sample"]=[
                        {"file":x.get("file"),"line":x.get("line")}
                        for x in data.get("matches",[])[:3]
                    ]
                if parsed:
                    entry["decoded_ok"]=parsed.get("ok")
                    entry["decoded_sequence"]=parsed.get("sequence")
                    entry["preview_only"]=bool(
                        (parsed.get("data") or {}).get("payload_truncated"))
                measurements.append(entry)
            # Capture actual sender logs and a separate virtual MCP receive
            # ledger for every command. No secrets or source content.
            if evidence:
                src=audit_root/"transport_audit.jsonl"
                if src.exists():
                    (evidence/"easychange_transport_audit.jsonl").write_bytes(src.read_bytes())
                with (evidence/"mcp_virtual_receive.jsonl").open("w",encoding="utf8") as fh:
                    for item in measurements:
                        fh.write(json.dumps({
                            "sequence":item["sequence"],
                            "command_type":item["command"].split()[0],
                            "easychange_ready":item["executed"],
                            "easychange_ok":item["service_ok"],
                            "visual_frame_present":item["qt_frame_has_envelope"],
                            "direct_crc_valid":item["direct_frame_crc_valid"],
                            "mcp_reply_received":item["virtual_mcp_received_reply"],
                            "mcp_crc_valid":item["ocr_crc_valid"],
                        })+"\n")
            return {
                "decoder":decoder,
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
    parser.add_argument("--mcp-source",default="")
    parser.add_argument("--tesseract",default="tesseract")
    parser.add_argument("--strict-ocr",action="store_true",
                        help="Fail if any simulated HDMI OCR result fails CRC32")
    parser.add_argument("--evidence-dir",default="")
    parser.add_argument("--max-cases",type=int,default=8)
    parser.add_argument("--start-case",type=int,default=1)
    parser.add_argument("--easychange-source",default="")
    parser.add_argument("--transport-mode",choices=["UBUNTU","QR","PIXEL","PIXEL+QR"],default="UBUNTU")
    args=parser.parse_args()
    report=run(mcp_source=args.mcp_source,ocr_binary=args.tesseract,
               evidence_dir=args.evidence_dir,max_cases=args.max_cases,
               start_case=args.start_case,easychange_source=args.easychange_source,
               transport_mode=args.transport_mode)
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    cases=report["reports"]
    # Safety-critical protocol regressions always fail CI.
    if not cases or any(not r["virtual_hid_confirmed"]
                        or not r["qt_frame_has_envelope"]
                        or not r["direct_frame_crc_valid"] for r in cases):
        raise SystemExit(2)
    if args.strict_ocr and any(not r["ocr_crc_valid"] for r in cases):
        raise SystemExit(3)
