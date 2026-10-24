"""Actual MCP EasyChange send/correlate with virtual Qt HID + framebuffer HDMI.

ONLY process-local fake devices; no COM, camera, network or live MCP daemon.
Separate execution-store and transport logs are confined to TemporaryDirectory.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sys
import tempfile
import threading
import time

def run(mcp_source: str, mode: str = "QR", command: str = ":state --compact",
        evidence_dir: str = "", timeout: float = 24.0,
        replay_identical: bool = False, tesseract: str = "",
        easychange_source: str = "") -> dict:
    # The real EasyChange CommandService supports process jobs and host
    # commands. Forbid those entirely: the lab must only touch disposable
    # source/read/search or its one allowlisted output file.
    allowed=(
        command == ":state --compact"
        or re.fullmatch(r":read (?:program\.py|large\.txt|saved\.txt) [1-9][0-9]{0,3} [1-9][0-9]{0,3}",command)
        or command in {":search greeting",":search Simulation"}
        or re.fullmatch(r":write saved\.txt [A-Za-z0-9_-]{1,100}",command)
    )
    if not allowed:
        raise ValueError("VIRTUAL_LAB_COMMAND_NOT_ALLOWLISTED")
    if easychange_source:
        source_root=Path(easychange_source).resolve()
        if not (source_root/"easychange"/"ui"/"main_window.py").is_file():
            raise FileNotFoundError("VIRTUAL_EASYCHANGE_SOURCE_NOT_FOUND")
        # Run the exact chosen EasyChange source without changing the live GUI.
        sys.path.insert(0,str(source_root))
    os.environ["QT_QPA_PLATFORM"]="offscreen"
    os.environ["QT_SCALE_FACTOR"]="1"
    import cv2
    import numpy as np
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage, QFontDatabase
    from easychange.core.workspace import Workspace
    from easychange.ui.main_window import MainWindow
    from easychange.core import transport_audit
    with tempfile.TemporaryDirectory(prefix="easychange_full_mcp_virtual_") as tmp:
        temp=Path(tmp)
        project=temp/"workspace"
        project.mkdir()
        (project/"program.py").write_text("def greeting(name):\n    return name\n",encoding="utf8")
        (project/"large.txt").write_text(
            "".join(f"{i:04d} Simulation with utf8 action {i}\n" for i in range(900)),
            encoding="utf8",
        )
        # Local caches/logs must never point at real MCP GUI or EasyChange logs.
        os.environ["LOCALAPPDATA"]=str(temp/"appdata")
        transport_audit.ROOT=temp/"ec_audit"
        transport_audit.FILE=transport_audit.ROOT/"transport_audit.jsonl"
        transport_audit._LAST_ID=0
        sys.path.insert(0,str(Path(mcp_source).resolve()))
        from mcp_server import easychange_tools as mcp
        # Mirror what the real MCP decoder actually collects (not just
        # what the Qt sender intended to display).
        decoded_indexes=set()
        decode_calls=[0]
        actual_decoder=mcp._decode_ec2_slots
        def track_decoder(*args,**kwargs):
            outcome=actual_decoder(*args,**kwargs)
            decode_calls[0]+=1
            if isinstance(outcome,tuple) and outcome:
                for packet in outcome[0] or []:
                    if (isinstance(packet,dict)
                            and packet.get("p")=="EC2"
                            and packet.get("s")=="QLABFULL01"
                            and type(packet.get("i")) is int):
                        decoded_indexes.add(packet["i"])
            return outcome
        mcp._decode_ec2_slots=track_decoder
        from app.easychange_execution_store import EasyChangeExecutionStore
        from app.easychange_transport_config import EasyChangeTransportConfig
        from app.optical_transaction_lock import OpticalTransactionLock
        from capture.local_vision import TesseractCLIProvider
        # CRITICAL: Never acquire the production-wide named optical mutex.
        # This is a distinct OS lock used by this one simulated process only.
        mcp._optical_transaction_lock=OpticalTransactionLock(
            name=f"BMO_VIRTUAL_OPTICAL_LAB_{os.getpid()}_{time.monotonic_ns()}"
        )
        mcp._host_execution_store=EasyChangeExecutionStore(root=temp/"mcp_store")
        EasyChangeTransportConfig.read=lambda self:{"mode":{"QR":"OPTICAL","PIXEL+QR":"HYBRID"}.get(mode,mode),"ui_mode":mode,"source":"virtual_lab"}
        mcp.remote_view_hint_supported=lambda:True
        mcp.remote_inline_dwell_supported=lambda:False
        mcp._session.update(
            ready=True,machine_mode=True,easychange_foreground=True,
            command_focus_assumed=True,command_focus_confirmed_at=time.time(),
            source_generation=1,capabilities_verified=True,protocol_version="EC2",
        )
        app=QApplication.instance() or QApplication([])
        if os.name=="nt":
            font=r"C:\Windows\Fonts\consola.ttf"
            if Path(font).is_file(): QFontDatabase.addApplicationFont(font)
        window=MainWindow(Workspace.open(project),machine_mode=True,hid_mode=True)
        window.setFixedSize(1280,720)
        window.show()
        app.processEvents()
        window.remote_transport_selector.setCurrentText(mode)
        app.processEvents()
        class Capture:
            def __init__(self):
                self.cond=threading.Condition()
                self.snapshot=None
                self.seq=0
                self.focus_confirmed=False
            def publish(self,frame,focused):
                with self.cond:
                    self.seq+=1
                    self.focus_confirmed=bool(focused)
                    self.snapshot={
                        "frame":frame,"sequence":self.seq,"source_generation":1,
                        "frame_id":f"virtual:{self.seq}","time":time.monotonic()
                    }
                    self.cond.notify_all()
            def get_latest(self,copy=False):
                with self.cond:
                    if self.snapshot is None:return None
                    d=self.snapshot.copy()
                d["age_ms"]=max(0,(time.monotonic()-d["time"])*1000)
                if copy:d["frame"]=d["frame"].copy()
                return d
            def wait_for_fresh_frame(self,after_sequence=None,timeout=0.3,min_frames=1,copy=False):
                cutoff=time.monotonic()+float(timeout)
                with self.cond:
                    target=int(after_sequence if after_sequence is not None else -1)
                    while self.snapshot is None or self.seq<target+min_frames:
                        remain=cutoff-time.monotonic()
                        if remain<=0:return{"ok":False,"error":"VIRTUAL_HDMI_TIMEOUT"}
                        self.cond.wait(remain)
                    return {"ok":True,"frame_id":self.snapshot["frame_id"],"sequence":self.seq}
            def signal_status(self):
                return {"status":"ACTIVE","pump_alive":True,"recovery_in_progress":False}

        capture=Capture()
        requests=queue.Queue()
        ocr_path=(tesseract or
                  (r"C:\Program Files\Tesseract-OCR\tesseract.exe"
                   if os.name=="nt" and Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe").is_file()
                   else shutil.which("tesseract") or "tesseract"))
        actual_ocr=TesseractCLIProvider(ocr_path)
        if mode=="UBUNTU" and not actual_ocr.available():
            raise RuntimeError("VIRTUAL_UBUNTU_TESSERACT_UNAVAILABLE")
        class Inspector:
            def ocr(self,frame,timeout,source,profile,force_fresh=True):
                from types import SimpleNamespace
                # Must correspond to THIS Qt GUI's actual machine/focus state;
                # this simulation does not claim that Tesseract recognized it.
                if ("before_hid_input" in source or
                    "auto_recover" in source or "easychange_prepare" in source):
                    text=("EasyChange|MACHINE|HID/COMPACT|FOCUS:COMMAND"
                        if capture.focus_confirmed else "")
                elif "ubuntu_terminal" in source:
                    # Unlike focus (sourced from the virtual Qt state), Ubuntu
                    # receipts MUST be recognized from real framebuffer pixels.
                    return actual_ocr.recognize(frame,timeout,profile=profile)
                else:
                    text=""
                return SimpleNamespace(text=text,confidence=1.0,duration_ms=0.1)
        class Controller:
            config=type("Config",(),{"ocr_timeout":1.0})()
            screen_inspector=Inspector()
            pause_manager=type("Pause",(),{"can_input":lambda self:True})()
            def type_text_via_esp(self,text,mode="fast"):
                done=threading.Event()
                requests.put((text,done))
                if not done.wait(timeout=12):
                    raise TimeoutError("VIRTUAL_ESP_EVENT_NOT_EXECUTED")
                return {"ok":True,"state":"completed","message_id":"LABHID"}
            def mcp_fast_snapshot(self):return {"capture_detail":{"frame_age_ms":12}}
        controller=Controller()
        controller.capture=capture
        def frame():
            px=window.grab()
            img=px.toImage().convertToFormat(QImage.Format.Format_RGB888)
            arr=np.frombuffer(img.bits(),dtype=np.uint8).reshape(img.height(),img.width(),3)
            return cv2.cvtColor(arr,cv2.COLOR_RGB2BGR).copy()
        captures=0
        def update_hdmi():
            nonlocal captures
            capture.publish(frame(),window._machine and window.command.hasFocus())
            captures+=1
        timer=QTimer()
        timer.setInterval(85)
        timer.timeout.connect(update_hdmi)
        timer.start()
        window.command.setFocus()
        app.processEvents()
        update_hdmi()
        sequence="QLABFULL01"
        started=time.monotonic()
        screenshot=None
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(mcp._send_and_correlate,controller,sequence,command,float(timeout))
            deadline=time.monotonic()+min(33.0,float(timeout)+11.0)
            sends=0
            next_status=time.monotonic()+5
            while not future.done() and time.monotonic()<deadline:
                if time.monotonic()>=next_status:
                    print("VIRTUAL_LAB_PROGRESS",json.dumps({
                        "seconds":int(time.monotonic()-started),
                        "hid":sends,
                        "frames":captures,
                        "last_result":getattr(window._last_machine_result,"sequence",None),
                        "page":getattr(window,"_optical_page",None),
                        "payloads":len(getattr(window,"_optical_payloads",[]) or []),
                        "pending":bool(getattr(window,"_optical_render_pending",None)),
                        "mcp_done":future.done(),
                        "mcp_received_indexes":sorted(decoded_indexes),
                        "mcp_qr_decode_calls":decode_calls[0],
                    }),file=sys.stderr,flush=True)
                    next_status=time.monotonic()+5
                app.processEvents()
                try: payload,done=requests.get_nowait()
                except queue.Empty:pass
                else:
                    window.command.setFocus(Qt.FocusReason.OtherFocusReason)
                    window.command.clear()
                    QTest.keyClicks(window.command,payload.strip())
                    QTest.keyClick(window.command,Qt.Key.Key_Return)
                    sends+=1
                    done.set()
                    app.processEvents()
                QTest.qWait(15)
            if not future.done():
                import traceback
                for ident,stack in sys._current_frames().items():
                    thread=next((t for t in threading.enumerate() if t.ident==ident),None)
                    if thread and "ThreadPool" in thread.name:
                        print("MCP_WORKER_STACK",thread.name,
                              "".join(traceback.format_stack(stack)[-12:]),
                              file=sys.stderr,flush=True)
                print("FULL_MCP_VIRTUAL_CALL_TIMEOUT",file=sys.stderr,flush=True)
                # Test subprocess is isolated and a blocked worker would make
                # ThreadPoolExecutor.__exit__ wait forever. Do not affect host.
                os._exit(124)
            result=future.result()
        replay_outcome=None
        sends_before_replay=sends
        if replay_identical:
            # The HOST execution store must answer without sending HID again.
            # At-most-once protection is verified against the live GUI audit.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(mcp._send_and_correlate,
                                   controller,sequence,command,5.0)
                replay_outcome=future.result(timeout=9.0)
            if not requests.empty():
                raise AssertionError("IDENTICAL_REPLAY_QUEUED_A_SECOND_HID_COMMAND")
        update_hdmi()
        screenshot=frame()
        timer.stop()
        app.processEvents()
        remote=window._last_machine_result
        actual_result=result.get("result")
        actual_data=actual_result.get("data") if isinstance(actual_result,dict) else None
        events=[]
        if transport_audit.FILE.exists():
            for audit_line in transport_audit.FILE.read_text(encoding="utf8").splitlines():
                try: events.append(json.loads(audit_line))
                except (ValueError,TypeError): pass
        original_ready=[x for x in events
                        if x.get("stage")=="ec1_result_ready"
                        and x.get("sequence")==sequence]
        original_lines=actual_data.get("lines") if isinstance(actual_data,dict) else None
        read_lines=[]
        if isinstance(original_lines,list):
            for item in original_lines:
                if isinstance(item,dict):
                    read_lines.append(item.get("text",item.get("content","")))
                elif isinstance(item,str):
                    read_lines.append(item)
        written=project/"saved.txt"
        report={
            "mode":mode,"command":command,"sequence":sequence,
            "virtual":True,"hid_sends":sends,"virtual_hdmi_frames":captures,
            "elapsed_ms":round((time.monotonic()-started)*1000,2),
            "remote_executed":bool(original_ready),
            "remote_ok":original_ready[-1].get("ok") if original_ready else None,
            "remote_execution_count":len(original_ready),
            "last_remote_result_sequence":getattr(remote,"sequence",None),
            "mcp_result_ok":result.get("ok"),"mcp_result_complete":result.get("result_complete"),
            "mcp_transport_state":result.get("transport_state"),
            "mcp_execution_state":result.get("execution_state"),
            "mcp_delivery_state":result.get("delivery_state"),
            "mcp_error":result.get("error"),"mcp_cause":result.get("cause"),
            "mcp_result_sequence":(result.get("result") or {}).get("sequence")
                if isinstance(result.get("result"),dict) else None,
            "mcp_parse_method":(result.get("frame") or {}).get("parse_method")
                if isinstance(result.get("frame"),dict) else None,
            "mcp_frame_error":(result.get("frame") or {}).get("error")
                if isinstance(result.get("frame"),dict) else None,
            "mcp_timing":result.get("timing_ms"),
            "disk_write":written.read_text(encoding="utf8") if written.exists() else None,
            "result_data_fields":sorted(actual_data) if isinstance(actual_data,dict) else [],
            "result_lines_count":(
                len(actual_data.get("lines")) if
                isinstance(actual_data,dict) and isinstance(actual_data.get("lines"),list)
                else None),
            "result_payload_truncated":(
                actual_data.get("payload_truncated")
                if isinstance(actual_data,dict) else None),
            "result_data_total":(
                actual_data.get("total") if isinstance(actual_data,dict) else None),
            "result_line_first":str(read_lines[0])[:120] if read_lines else None,
            "result_line_last":str(read_lines[-1])[:120] if read_lines else None,
            "result_lines_identical_to_disk":(
                [line.split("|",1)[1] if "|" in line else line
                 for line in read_lines]
                == (project/"large.txt").read_text(encoding="utf8").splitlines()[
                    int(command.split()[2])-1:
                    int(command.split()[2])-1+int(command.split()[3])]
                if command.startswith(":read large.txt") else None),
            "same_sequence_replay_issued":bool(replay_identical),
            "same_sequence_replay_from_host":(
                replay_outcome is not None
                and replay_outcome.get("ok") is True
                and replay_outcome.get("remote_execution_skipped") is True
                and sends==sends_before_replay
            ) if replay_identical else None,
            "same_sequence_replay_sent_new_hid": (
                sends!=sends_before_replay or not requests.empty()
                if replay_identical else None),
            "exact_write_confirmed":(
                written.is_file() and written.read_text(encoding="utf8")
                == command.split(maxsplit=2)[2]
                if command.startswith(":write saved.txt ") else None),
        }
        if evidence_dir:
            folder=Path(evidence_dir).resolve()
            folder.mkdir(parents=True,exist_ok=True)
            cv2.imwrite(str(folder/"full_mcp_virtual.png"),screenshot)
            (folder/"full_mcp_report.json").write_text(json.dumps(report,indent=2),encoding="utf8")
            for f,dest in ((transport_audit.FILE,"virtual_ec_audit.jsonl"),):
                if f.is_file(): (folder/dest).write_bytes(f.read_bytes())
        window.close()
        app.processEvents()
        return report

def main():
    arg=argparse.ArgumentParser()
    arg.add_argument("--mcp-source",required=True)
    arg.add_argument("--mode",choices=["QR","PIXEL","PIXEL+QR","UBUNTU"],default="QR")
    arg.add_argument("--command",default=":state --compact")
    arg.add_argument("--evidence-dir",default="")
    arg.add_argument("--timeout",type=float,default=24)
    arg.add_argument("--replay-identical",action="store_true")
    arg.add_argument("--tesseract",default="")
    arg.add_argument("--easychange-source",default="")
    args=arg.parse_args()
    result=run(args.mcp_source,args.mode,args.command,args.evidence_dir,
               args.timeout,args.replay_identical,args.tesseract,
               args.easychange_source)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if not result["mcp_result_complete"]:raise SystemExit(2)
    if not result["remote_executed"] or result["remote_execution_count"]!=1:
        raise SystemExit(3)
    if result["exact_write_confirmed"] is False:
        raise SystemExit(4)
    if result["result_lines_identical_to_disk"] is False:
        raise SystemExit(5)
    if args.replay_identical and not result["same_sequence_replay_from_host"]:
        raise SystemExit(6)

if __name__=="__main__":
    main()
